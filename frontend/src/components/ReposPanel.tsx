import { useCallback, useEffect, useState } from "react";
import { addRepo, deleteRepo, fetchRepos, updateRepo, type RepoPayload } from "../api";
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

// When editing an existing repo we pre-fill url/branch/path/auth but leave the
// secret fields blank — a blank secret means "keep whatever is stored".
function formFromRepo(r: RepoInfo): RepoPayload {
  return {
    url: r.url, branch: r.branch, path: r.path, authType: r.authType,
    token: "", sshKey: "", githubAppId: "", githubInstallationId: "", githubPrivateKey: "",
  };
}

export function ReposPanel({ canAdmin, onClose, onChanged }: Props) {
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [form, setForm] = useState<RepoPayload>(EMPTY_FORM);
  // null = the "add" form; a number = editing that repo id.
  const [editingId, setEditingId] = useState<number | null>(null);
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

  const resetForm = () => {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setError(null);
  };

  const onEdit = (repo: RepoInfo) => {
    setEditingId(repo.id);
    setForm(formFromRepo(repo));
    setError(null);
  };

  const onSave = async () => {
    if (!form.url.trim()) {
      setError("repository URL is required");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (editingId === null) {
        await addRepo(form);
      } else {
        // Only send secrets that were actually typed, so a blank field keeps the
        // stored credential instead of wiping it.
        const patch: Partial<RepoPayload> = {
          url: form.url, branch: form.branch, path: form.path, authType: form.authType,
          githubAppId: form.githubAppId, githubInstallationId: form.githubInstallationId,
        };
        if (form.token) patch.token = form.token;
        if (form.sshKey) patch.sshKey = form.sshKey;
        if (form.githubPrivateKey) patch.githubPrivateKey = form.githubPrivateKey;
        await updateRepo(editingId, patch);
      }
      resetForm();
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
      if (editingId === repo.id) resetForm();
      await load();
      onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  const set = (field: keyof RepoPayload) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
      setForm({ ...form, [field]: e.target.value });

  const isEditing = editingId !== null;
  const secretPlaceholderSuffix = isEditing ? " — leave blank to keep current" : "";

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
            <h3>{isEditing ? "Edit repository" : "Connect a repository"}</h3>
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
                  placeholder={`access token (optional, for private repos)${secretPlaceholderSuffix}`}
                  value={form.token}
                  onChange={set("token")}
                />
              )}
              {form.authType === "ssh" && (
                <textarea
                  rows={5}
                  placeholder={isEditing
                    ? "paste a new SSH private key to replace the stored one — leave blank to keep current"
                    : "-----BEGIN OPENSSH PRIVATE KEY-----\n… private key with read access to the repo …"}
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
                    placeholder={isEditing
                      ? "paste a new GitHub App private key (.pem) to replace the stored one — leave blank to keep current"
                      : "-----BEGIN RSA PRIVATE KEY-----\n… GitHub App private key (.pem) …"}
                    value={form.githubPrivateKey}
                    onChange={set("githubPrivateKey")}
                  />
                </>
              )}

              <div className="repo-form-row">
                <button
                  className="btn primary"
                  onClick={onSave}
                  disabled={busy || !canAdmin}
                  title={canAdmin ? "" : "requires admin role"}
                >
                  {busy ? (isEditing ? "Saving…" : "Connecting…") : isEditing ? "Save changes" : "Connect"}
                </button>
                {isEditing && (
                  <button className="btn" onClick={resetForm} disabled={busy}>
                    Cancel
                  </button>
                )}
              </div>
            </div>
          </section>

          <section>
            <h3>Tracked repositories ({repos.length})</h3>
            {repos.length === 0 && <div className="muted">No repositories connected yet.</div>}
            {repos.map((r) => (
              <div key={r.id} className={`repo-entry ${editingId === r.id ? "editing" : ""}`}>
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
                <div className="panel-actions">
                  <button
                    className="btn"
                    disabled={!canAdmin}
                    title={canAdmin ? "" : "requires admin role"}
                    onClick={() => onEdit(r)}
                  >
                    Edit
                  </button>
                  <button
                    className="btn danger"
                    disabled={!canAdmin}
                    title={canAdmin ? "" : "requires admin role"}
                    onClick={() => onDelete(r)}
                  >
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </section>
        </div>
      </div>
    </div>
  );
}

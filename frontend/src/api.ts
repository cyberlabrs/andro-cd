import type { AppDetail, AppSummary, AuditEntry, AuthInfo, AwsProfile, DiffDocument, HistoryEntry, LogsResponse, RepoInfo, Resources, RevisionInfo, ServerStatus } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body.slice(0, 300)}`);
  }
  return res.json();
}

export const fetchMe = () => request<AuthInfo>("/api/auth/me");
export const logout = () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
export const fetchStatus = () => request<ServerStatus>("/api/status");
export const fetchApps = () => request<AppSummary[]>("/api/apps");
export const fetchApp = (name: string) => request<AppDetail>(`/api/apps/${encodeURIComponent(name)}`);
export const syncApp = (name: string) =>
  request<AppDetail>(`/api/apps/${encodeURIComponent(name)}/sync`, { method: "POST" });
export const refreshAll = () => request<ServerStatus>("/api/refresh", { method: "POST" });
export const fetchHistory = (name: string) =>
  request<HistoryEntry[]>(`/api/apps/${encodeURIComponent(name)}/history`);
export const fetchRepos = () => request<RepoInfo[]>("/api/repos");
export interface RepoPayload {
  url: string;
  branch: string;
  path: string;
  authType: string;
  token: string;
  sshKey: string;
  githubAppId: string;
  githubInstallationId: string;
  githubPrivateKey: string;
}

export const addRepo = (data: RepoPayload) =>
  request<RepoInfo>("/api/repos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
export const deleteRepo = (id: number) =>
  request<{ deleted: number }>(`/api/repos/${id}`, { method: "DELETE" });
export const fetchDiff = (name: string) =>
  request<DiffDocument>(`/api/apps/${encodeURIComponent(name)}/diff`);
export const fetchProfiles = () => request<AwsProfile[]>("/api/profiles");
export const addProfile = (data: { name: string; region: string; accessKeyId: string; secretAccessKey: string }) =>
  request<AwsProfile>("/api/profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
export const deleteProfile = (name: string) =>
  request<{ deleted: string }>(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
export const fetchRevisions = (name: string) =>
  request<RevisionInfo[]>(`/api/apps/${encodeURIComponent(name)}/revisions`);
export const rollbackTo = (name: string, revision: number) =>
  request<AppDetail>(`/api/apps/${encodeURIComponent(name)}/rollback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision }),
  });
export const pruneApp = (name: string) =>
  request<AppDetail>(`/api/apps/${encodeURIComponent(name)}/prune`, { method: "POST" });
export const fetchResources = (name: string) =>
  request<Resources>(`/api/apps/${encodeURIComponent(name)}/resources`);
export const logStreamUrl = (name: string, container?: string) =>
  `/api/apps/${encodeURIComponent(name)}/logs/stream` +
  (container ? `?container=${encodeURIComponent(container)}` : "");
export const fetchAudit = (limit = 100, action?: string) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (action) params.set("action", action);
  return request<AuditEntry[]>(`/api/audit?${params}`);
};
export const fetchLogs = (name: string, container?: string, lines = 100) => {
  const params = new URLSearchParams({ lines: String(lines) });
  if (container) params.set("container", container);
  return request<LogsResponse>(`/api/apps/${encodeURIComponent(name)}/logs?${params}`);
};

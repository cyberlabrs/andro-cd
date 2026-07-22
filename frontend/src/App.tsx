import { useCallback, useEffect, useRef, useState } from "react";
import { fetchApps, fetchMe, fetchStatus, logout, refreshAll } from "./api";
import type { AppSummary, AuthInfo, ServerStatus } from "./types";
import { AppCard } from "./components/AppCard";
import { AppDetailPanel } from "./components/AppDetailPanel";
import { AuditPanel } from "./components/AuditPanel";
import { DocsPage } from "./components/DocsPage";
import { LoginPage } from "./components/LoginPage";
import { ProfilesPanel } from "./components/ProfilesPanel";
import { ReposPanel } from "./components/ReposPanel";

// Bug #29: configurable poll interval so operators can dial back on large fleets.
// Set VITE_POLL_MS=10000 (or higher) before build to reduce API/AWS load.
const POLL_MS = Number((import.meta as any).env?.VITE_POLL_MS) || 5000;

type StatusFilter = "all" | "Synced" | "OutOfSync" | "attention";
type SortKey = "name" | "status" | "health" | "recent";

const SYNC_RANK: Record<string, number> = { Error: 0, OutOfSync: 1, Syncing: 2, Orphaned: 3, Unknown: 4, Synced: 5 };
const HEALTH_RANK: Record<string, number> = { Degraded: 0, Progressing: 1, Unknown: 2, Healthy: 3 };

function initialParams() {
  const p = new URLSearchParams(window.location.search);
  const filter = p.get("filter");
  const sort = p.get("sort");
  return {
    query: p.get("q") ?? "",
    statusFilter: (["all", "Synced", "OutOfSync", "attention"].includes(filter ?? "")
      ? filter : "all") as StatusFilter,
    sortKey: (["name", "status", "health", "recent"].includes(sort ?? "")
      ? sort : "name") as SortKey,
    selected: p.get("app"),
  };
}

function initialTheme(): "light" | "dark" {
  const saved = localStorage.getItem("androcd-theme");
  if (saved === "dark" || saved === "light") return saved;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function App() {
  const init = useRef(initialParams()).current;
  const [auth, setAuth] = useState<AuthInfo | null>(null);
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [selected, setSelected] = useState<string | null>(init.selected);
  const [showRepos, setShowRepos] = useState(false);
  const [showProfiles, setShowProfiles] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const [showDocs, setShowDocs] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState(init.query);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(init.statusFilter);
  const [sortKey, setSortKey] = useState<SortKey>(init.sortKey);
  const [theme, setTheme] = useState<"light" | "dark">(initialTheme);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    fetchMe().then(setAuth).catch((e) => setError(String(e)));
  }, []);

  // Theme: applied on <html> so overlays/portals inherit; persisted per browser.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("androcd-theme", theme);
  }, [theme]);

  // Persist search/filter/sort/selected app in the URL — shareable, survives reload.
  useEffect(() => {
    const p = new URLSearchParams();
    if (query) p.set("q", query);
    if (statusFilter !== "all") p.set("filter", statusFilter);
    if (sortKey !== "name") p.set("sort", sortKey);
    if (selected) p.set("app", selected);
    const qs = p.toString();
    window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
  }, [query, statusFilter, sortKey, selected]);

  // Keyboard shortcuts: "/" focuses search, Escape closes any open panel.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
      if (e.key === "/" && !typing) {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "Escape") {
        setSelected(null);
        setShowRepos(false);
        setShowProfiles(false);
        setShowAudit(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const authenticated = auth?.authenticated ?? false;

  const load = useCallback(async () => {
    try {
      const [a, s] = await Promise.all([fetchApps(), fetchStatus()]);
      setApps(a);
      setStatus(s);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    if (!authenticated) return;
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load, authenticated]);

  const onLogout = async () => {
    await logout().catch(() => undefined);
    setAuth(await fetchMe());
  };

  if (auth === null) {
    // Bug #27: neutral background while auth resolves (avoids brief dark banner).
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span className="muted">Loading…</span>
      </div>
    );
  }
  if (!authenticated) {
    return <LoginPage />;
  }

  if (showDocs) {
    return <DocsPage onClose={() => setShowDocs(false)} />;
  }

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      await refreshAll();
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setRefreshing(false);
    }
  };

  const failingRepos = status?.repos.filter((r) => r.error) ?? [];
  const role = auth.role ?? "viewer";
  const canOperate = auth.mode === "none" || role === "operator" || role === "admin";
  const canAdmin = auth.mode === "none" || role === "admin";

  const filtered = apps.filter((a) => {
    const labelText = Object.entries(a.labels ?? {}).map(([k, v]) => `${k}=${v}`).join(" ");
    if (query && !`${a.name} ${a.cluster ?? ""} ${a.images.join(" ")} ${labelText}`.toLowerCase().includes(query.toLowerCase())) {
      return false;
    }
    if (statusFilter === "all") return true;
    if (statusFilter === "attention") {
      return a.syncStatus === "Error" || a.syncStatus === "OutOfSync" || a.health === "Degraded";
    }
    return a.syncStatus === statusFilter;
  });

  const sorted = [...filtered].sort((a, b) => {
    switch (sortKey) {
      case "status": {
        const d = (SYNC_RANK[a.syncStatus] ?? 9) - (SYNC_RANK[b.syncStatus] ?? 9);
        return d !== 0 ? d : a.name.localeCompare(b.name);
      }
      case "health": {
        const d = (HEALTH_RANK[a.health] ?? 9) - (HEALTH_RANK[b.health] ?? 9);
        return d !== 0 ? d : a.name.localeCompare(b.name);
      }
      case "recent": {
        const ta = a.lastSynced ? Date.parse(a.lastSynced) : 0;
        const tb = b.lastSynced ? Date.parse(b.lastSynced) : 0;
        return tb !== ta ? tb - ta : a.name.localeCompare(b.name);
      }
      default:
        return a.name.localeCompare(b.name);
    }
  });

  const counts = {
    all: apps.length,
    Synced: apps.filter((a) => a.syncStatus === "Synced").length,
    OutOfSync: apps.filter((a) => a.syncStatus === "OutOfSync").length,
    attention: apps.filter(
      (a) => a.syncStatus === "Error" || a.syncStatus === "OutOfSync" || a.health === "Degraded"
    ).length,
  };

  return (
    <div className="layout">
      <header className="header">
        <div className="brand">
          <span className="logo">⬢</span>
          <h1>Andro-CD</h1>
          <span className="subtitle">GitOps for AWS ECS</span>
          {status?.version && status.version !== "dev" && (
            <span className="commit">v{status.version}</span>
          )}
        </div>
        <div className="header-right">
          {status && (
            <span className="muted">
              {status.appCount} apps · {status.repos.length} repos
            </span>
          )}
          {status?.lastPoll && (
            <span className="muted">last poll {new Date(status.lastPoll).toLocaleTimeString()}</span>
          )}
          <button className="btn" onClick={() => setShowRepos(true)}>Repositories</button>
          <button className="btn" onClick={() => setShowProfiles(true)}>AWS Profiles</button>
          {canAdmin && <button className="btn" onClick={() => setShowAudit(true)}>Audit</button>}
          <button className="btn" onClick={() => setShowDocs(true)}>Docs</button>
          <button className="btn" onClick={onRefresh} disabled={refreshing || !canOperate}>
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
          <button
            className="btn icon"
            title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
          {auth.mode === "github" && auth.user && (
            <span className="user-chip">
              {auth.user.avatar && <img className="avatar" src={auth.user.avatar} alt="" />}
              <span>{auth.user.login}</span>
              <span className="badge sync-unknown">{role}</span>
              <button className="btn" onClick={onLogout}>Logout</button>
            </span>
          )}
        </div>
      </header>

      {error && <div className="banner error">{error}</div>}
      {status?.dryRun && (
        <div className="banner warn">
          <b>Dry-run mode</b> — syncs record the plan but nothing is applied to AWS
          (<span className="mono">DRY_RUN=true</span>).
        </div>
      )}
      {status && !status.leader && (
        <div className="banner warn">
          This replica is a <b>standby</b> — another replica holds the leader lock and applies
          changes. The view stays live; manual actions still work from here.
        </div>
      )}
      {failingRepos.length > 0 && (
        <div className="banner error">
          {failingRepos.length} repositor{failingRepos.length === 1 ? "y is" : "ies are"} failing to sync
          — open <a onClick={() => setShowRepos(true)}>Repositories</a> for details.
        </div>
      )}

      <div className="toolbar">
        <input
          ref={searchRef}
          className="search"
          placeholder="Search apps, clusters, images…  ( / )"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="filter-chips">
          {([
            ["all", `All ${counts.all}`],
            ["Synced", `Synced ${counts.Synced}`],
            ["OutOfSync", `OutOfSync ${counts.OutOfSync}`],
            ["attention", `Needs attention ${counts.attention}`],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              className={`chip ${statusFilter === key ? "active" : ""}`}
              onClick={() => setStatusFilter(key)}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="sort-control">
          <span className="muted">Sort</span>
          <select value={sortKey} onChange={(e) => setSortKey(e.target.value as SortKey)}>
            <option value="name">name</option>
            <option value="status">sync status</option>
            <option value="health">health</option>
            <option value="recent">recently synced</option>
          </select>
        </label>
      </div>

      <main className="grid">
        {apps.length === 0 && (
          <div className="empty">
            {status && status.repos.length === 0 ? (
              <>
                No repositories connected.{" "}
                <a onClick={() => setShowRepos(true)}>Connect a Git repository</a> with
                ECSService manifests to get started.
              </>
            ) : (
              "No applications found in the connected repositories."
            )}
          </div>
        )}
        {apps.length > 0 && sorted.length === 0 && (
          <div className="empty">No applications match the current filter.</div>
        )}
        {sorted.map((a) => (
          <AppCard
            key={a.name}
            app={a}
            onClick={() => setSelected(a.name)}
            onLabelClick={(label) => setQuery(label)}
          />
        ))}
      </main>

      {selected && (
        <AppDetailPanel
          name={selected}
          canOperate={canOperate}
          onClose={() => setSelected(null)}
          onChanged={load}
        />
      )}
      {showRepos && (
        <ReposPanel canAdmin={canAdmin} onClose={() => setShowRepos(false)} onChanged={load} />
      )}
      {showProfiles && (
        <ProfilesPanel canAdmin={canAdmin} onClose={() => setShowProfiles(false)} />
      )}
      {showAudit && <AuditPanel onClose={() => setShowAudit(false)} />}
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { fetchApp, fetchHistory, fetchResources, pruneApp, syncApp } from "../api";
import type { AppDetail, HistoryEntry, Resources } from "../types";
import { HealthBadge, SyncBadge } from "./StatusBadge";
import { DiffTab } from "./DiffTab";
import { LogsTab } from "./LogsTab";
import { OverviewTab } from "./OverviewTab";
import { StoppedTasksSection, TaskDefTab, TasksTab } from "./ResourceTabs";
import { Timeline } from "./Timeline";

interface Props {
  name: string;
  canOperate: boolean;
  onClose: () => void;
  onChanged: () => void;
}

const TABS = ["Overview", "Diff", "Task Definition", "Tasks", "Logs", "History", "Manifest"] as const;
type Tab = (typeof TABS)[number];

export function AppDetailPanel({ name, canOperate, onClose, onChanged }: Props) {
  const [app, setApp] = useState<AppDetail | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [resources, setResources] = useState<Resources | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [a, h] = await Promise.all([fetchApp(name), fetchHistory(name)]);
      setApp(a);
      setHistory(h);
    } catch (e) {
      setError(String(e));
    }
  }, [name]);

  const loadResources = useCallback(async () => {
    try {
      setResources(await fetchResources(name));
    } catch (e) {
      setResources({ error: String(e), cluster: null, service: null, taskDefinition: null, tasks: [] });
    }
  }, [name]);

  useEffect(() => {
    load();
    loadResources();
    const t1 = setInterval(load, 5000);
    const t2 = setInterval(loadResources, 10000);
    return () => {
      clearInterval(t1);
      clearInterval(t2);
    };
  }, [load, loadResources]);

  const onSync = async () => {
    setSyncing(true);
    setError(null);
    try {
      setApp(await syncApp(name));
      onChanged();
      loadResources();
    } catch (e) {
      setError(String(e));
    } finally {
      setSyncing(false);
    }
  };

  return (
    <div className="overlay" onClick={onClose}>
      <div className="panel panel-wide" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <div className="panel-title">
            <h2>{name}</h2>
            {app && (
              <div className="badges">
                <SyncBadge status={app.syncStatus} />
                <HealthBadge health={app.health} />
              </div>
            )}
          </div>
          <div className="panel-actions">
            {app?.syncStatus === "Orphaned" && (
              <button
                className="btn danger"
                disabled={!canOperate}
                onClick={async () => {
                  if (!window.confirm(`Delete the ECS service for '${name}' from AWS? The app was removed from git.`)) return;
                  try {
                    await pruneApp(name);
                    onChanged();
                    onClose();
                  } catch (e) {
                    setError(String(e));
                  }
                }}
              >
                Prune
              </button>
            )}
            <button
              className="btn primary"
              onClick={onSync}
              disabled={syncing || !canOperate}
              title={canOperate ? "" : "requires operator role"}
            >
              {syncing ? "Syncing…" : app?.syncPaused ? "Sync (resume auto-sync)" : "Sync"}
            </button>
            <button className="btn" onClick={onClose}>✕</button>
          </div>
        </div>

        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t}
              className={`tab ${tab === t ? "active" : ""}`}
              onClick={() => setTab(t)}
            >
              {t}
              {t === "Tasks" && resources && resources.tasks.length > 0 && (
                <span className="tab-count">{resources.tasks.length}</span>
              )}
            </button>
          ))}
        </nav>

        {error && <div className="banner error">{error}</div>}
        {!app && !error && <div className="muted">Loading…</div>}

        {app && (
          <div className="panel-body">
            {tab === "Overview" && (
              <OverviewTab app={app} resources={resources} />
            )}
            {tab === "Diff" && <DiffTab appName={name} />}
            {tab === "Task Definition" && (
              <TaskDefTab
                resources={resources}
                appName={name}
                canOperate={canOperate}
                onRolledBack={() => {
                  load();
                  loadResources();
                  onChanged();
                }}
              />
            )}
            {tab === "Tasks" && (
              <>
                <TasksTab resources={resources} />
                <StoppedTasksSection resources={resources} />
              </>
            )}
            {tab === "Logs" && <LogsTab appName={name} />}
            {tab === "History" && (
              <section>
                <Timeline entries={history} />
              </section>
            )}
            {tab === "Manifest" && (
              <section>
                <div className="muted" style={{ marginBottom: 8 }}>
                  {app.repo && `${app.repo} · `}{app.file}
                </div>
                <pre className="manifest">{JSON.stringify(app.manifest, null, 2)}</pre>
              </section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

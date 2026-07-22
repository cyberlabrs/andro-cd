import { useEffect, useRef, useState } from "react";
import { logStreamUrl } from "../api";
import type { LogLine } from "../types";

const MAX_LINES = 2000;

export function LogsTab({ appName }: { appName: string }) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [containers, setContainers] = useState<string[]>([]);
  const [activeContainer, setActiveContainer] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | undefined>(undefined);
  const [group, setGroup] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [connected, setConnected] = useState(false);
  const [follow, setFollow] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (paused) {
      setConnected(false);
      return;
    }
    setError(null);
    // Bug #39: `withCredentials: true` guarantees the session cookie is sent even
    // when the UI is served from a different origin than the API (e.g. Vite dev proxy).
    const es = new EventSource(logStreamUrl(appName, selected), { withCredentials: true });
    es.onopen = () => setConnected(true);
    es.onerror = () => {
      // Bug #16: close the stream on the first error and let a fresh `useEffect` reopen it
      // on user action (Pause/Resume/container switch). Browsers' auto-reconnect can
      // spin quickly and double-count events.
      setConnected(false);
      es.close();
    };
    es.onmessage = (ev) => {
      const line = JSON.parse(ev.data) as LogLine;
      setLines((prev) => [...prev.slice(-(MAX_LINES - 1)), line]);
    };
    es.addEventListener("meta", (ev) => {
      const meta = JSON.parse((ev as MessageEvent).data);
      setContainers(meta.containers ?? []);
      setActiveContainer(meta.container ?? null);
      setGroup(meta.group ?? null);
    });
    es.addEventListener("error", (ev) => {
      const data = (ev as MessageEvent).data;
      if (data) {
        setError(JSON.parse(data).error);
        es.close();
        setConnected(false);
      }
    });
    return () => es.close();
  }, [appName, selected, paused]);

  useEffect(() => {
    if (follow && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [lines, follow]);

  const switchContainer = (c: string) => {
    setLines([]);
    setSelected(c);
  };

  return (
    <div className="logs-tab">
      <div className="logs-controls">
        <span className={`live-dot ${connected ? "on" : ""}`} title={connected ? "streaming" : "disconnected"} />
        <span className="muted">{connected ? "live" : paused ? "paused" : "connecting…"}</span>
        {containers.length > 1 && (
          <select value={selected ?? activeContainer ?? ""} onChange={(e) => switchContainer(e.target.value)}>
            {containers.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        )}
        <label className="follow-label">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          follow
        </label>
        <button className="btn" onClick={() => setPaused(!paused)}>
          {paused ? "Resume" : "Pause"}
        </button>
        <button className="btn" onClick={() => setLines([])}>Clear</button>
        {group && <span className="muted log-group">{group}</span>}
      </div>

      {error && <div className="banner error">{error}</div>}

      <div className="logs logs-stream" ref={boxRef}>
        {lines.length === 0 && !error && (
          <span className="muted">waiting for log events…</span>
        )}
        {lines.map((l, i) => (
          <div key={i} className="log-line">
            <span className="log-ts">{l.timestamp.slice(11, 19)}</span>
            <span className="log-msg">{l.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

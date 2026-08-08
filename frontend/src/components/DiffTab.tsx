import { useEffect, useMemo, useRef, useState } from "react";
import { fetchDiff } from "../api";
import type { DiffDocument } from "../types";

type Chunk = { type: "eq" | "del" | "add"; live?: string; desired?: string };

// --- YAML dump (minimal, block-style, sufficient for our normalized payload) ----
// Handles dict/list/string/number/bool/null. Multi-line strings become | folded.
// Keys and values that would confuse YAML get quoted. Not a full spec impl —
// suitable ONLY because our diff payload is always JSON-typed data.
function yamlDump(value: unknown, indent = 0): string {
  const pad = "  ".repeat(indent);
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  if (typeof value === "string") return quoteIfNeeded(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    return value.map((item) => {
      const rendered = yamlDump(item, indent + 1);
      if (typeof item === "object" && item !== null && !Array.isArray(item) && Object.keys(item).length) {
        // First key inline with the "- ", remaining keys indented under it.
        const [firstLine, ...rest] = rendered.split("\n");
        const firstStripped = firstLine.replace(/^ +/, "");
        const tail = rest.length ? "\n" + rest.join("\n") : "";
        return `${pad}- ${firstStripped}${tail}`;
      }
      return `${pad}- ${rendered.replace(/^\s+/, "")}`;
    }).join("\n");
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return "{}";
    return entries.map(([k, v]) => {
      const key = quoteKey(k);
      if (v === null || v === undefined) return `${pad}${key}: null`;
      if (typeof v !== "object") return `${pad}${key}: ${yamlDump(v)}`;
      if (Array.isArray(v) && v.length === 0) return `${pad}${key}: []`;
      if (!Array.isArray(v) && Object.keys(v as object).length === 0) return `${pad}${key}: {}`;
      // For lists we DON'T indent further — items already carry `pad`.
      const childIndent = Array.isArray(v) ? indent : indent + 1;
      return `${pad}${key}:\n${yamlDump(v, childIndent)}`;
    }).join("\n");
  }
  return String(value);
}

function quoteIfNeeded(s: string): string {
  if (s === "") return '""';
  if (/[:#\n\[\]{}&*!|>'"%@`,]/.test(s) || /^[\s-?]/.test(s) || /\s$/.test(s)
      || /^(true|false|null|yes|no|on|off)$/i.test(s) || /^-?\d/.test(s)) {
    return JSON.stringify(s);
  }
  return s;
}

function quoteKey(k: string): string {
  return /^[A-Za-z_][\w.-]*$/.test(k) ? k : JSON.stringify(k);
}

// --- LCS-based diff over line arrays ---------------------------------------------
function diffLines(live: string[], desired: string[]): Chunk[] {
  const n = live.length;
  const m = desired.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = live[i] === desired[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const chunks: Chunk[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (live[i] === desired[j]) {
      chunks.push({ type: "eq", live: live[i], desired: desired[j] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      chunks.push({ type: "del", live: live[i] });
      i++;
    } else {
      chunks.push({ type: "add", desired: desired[j] });
      j++;
    }
  }
  while (i < n) chunks.push({ type: "del", live: live[i++] });
  while (j < m) chunks.push({ type: "add", desired: desired[j++] });
  return chunks;
}

// --- Minimal YAML syntax highlight ------------------------------------------------
// A dumb tokenizer sufficient for what yamlDump emits: key: value, - list item.
function highlightYaml(line: string): { cls: string; text: string }[] {
  // "  - key: value"  or  "  key: value"  or  "  - value"  or  "  value"
  const match = line.match(/^(\s*(?:-\s+)?)([A-Za-z_][\w.-]*|"[^"]*")(:)(\s.*)?$/);
  if (match) {
    const [, lead, key, colon, rest] = match;
    return [
      { cls: "y-lead", text: lead },
      { cls: "y-key", text: key },
      { cls: "y-colon", text: colon },
      ...(rest ? [{ cls: valueClass(rest.trimStart()), text: rest }] : []),
    ];
  }
  const listMatch = line.match(/^(\s*-\s+)(.*)$/);
  if (listMatch) {
    return [
      { cls: "y-lead", text: listMatch[1] },
      { cls: valueClass(listMatch[2]), text: listMatch[2] },
    ];
  }
  return [{ cls: valueClass(line.trimStart()), text: line }];
}

function valueClass(v: string): string {
  if (/^(true|false|null|yes|no)$/i.test(v)) return "y-bool";
  if (/^-?\d+(\.\d+)?$/.test(v)) return "y-num";
  if (v.startsWith('"')) return "y-str";
  return "y-val";
}

function renderLine(text: string | undefined): JSX.Element[] {
  if (!text) return [<span key="empty">{" "}</span>];
  return highlightYaml(text).map((tok, i) => (
    <span key={i} className={tok.cls}>{tok.text}</span>
  ));
}

// --- Component --------------------------------------------------------------------
export function DiffTab({ appName }: { appName: string }) {
  const [doc, setDoc] = useState<DiffDocument | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hideEqual, setHideEqual] = useState(false);
  const [format, setFormat] = useState<"yaml" | "json">("yaml");
  const liveRef = useRef<HTMLPreElement>(null);
  const desiredRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    fetchDiff(appName)
      .then((d) => {
        setDoc(d);
        if (d.error) setError(d.error);
      })
      .catch((e) => setError(String(e)));
  }, [appName]);

  const chunks = useMemo<Chunk[]>(() => {
    if (!doc) return [];
    const render = (obj: unknown) =>
      format === "yaml" ? yamlDump(obj ?? {}) : JSON.stringify(obj ?? {}, null, 2);
    const live = render(doc.live).split("\n");
    const desired = render(doc.desired).split("\n");
    return diffLines(live, desired);
  }, [doc, format]);

  // Group consecutive "eq" chunks so we can collapse them behind a "… N unchanged lines" button.
  const groups = useMemo(() => collapseEqual(chunks, hideEqual), [chunks, hideEqual]);

  // Two-way scroll sync — write both scrollTops when either pane fires scroll.
  const syncing = useRef(false);
  const bindScroll = (source: React.RefObject<HTMLPreElement>, target: React.RefObject<HTMLPreElement>) =>
    () => {
      if (syncing.current || !source.current || !target.current) return;
      syncing.current = true;
      target.current.scrollTop = source.current.scrollTop;
      target.current.scrollLeft = source.current.scrollLeft;
      requestAnimationFrame(() => (syncing.current = false));
    };

  if (error) return <div className="banner error">{error}</div>;
  if (!doc) return <div className="muted">Computing diff…</div>;

  const hasChanges = chunks.some((c) => c.type !== "eq");
  const addCount = chunks.filter((c) => c.type === "add").length;
  const delCount = chunks.filter((c) => c.type === "del").length;

  return (
    <section>
      <div className="diff-toolbar">
        <span className="diff-stats">
          <span className="diff-stat-add">+{addCount}</span>
          <span className="diff-stat-del">−{delCount}</span>
          {!hasChanges && <span className="muted"> · no differences</span>}
        </span>
        <div className="diff-toolbar-right">
          <label className="follow-label">
            <input type="checkbox" checked={hideEqual} onChange={(e) => setHideEqual(e.target.checked)} />
            hide unchanged
          </label>
          <select value={format} onChange={(e) => setFormat(e.target.value as "yaml" | "json")}>
            <option value="yaml">YAML</option>
            <option value="json">JSON</option>
          </select>
        </div>
      </div>

      <div className="diff-cols">
        <div className="diff-col">
          <h3>Live (AWS)</h3>
          <pre className="manifest diff-pane" ref={liveRef} onScroll={bindScroll(liveRef, desiredRef)}>
            {groups.map((g, i) => renderColumn(g, i, "live"))}
          </pre>
        </div>
        <div className="diff-col">
          <h3>Desired (Git)</h3>
          <pre className="manifest diff-pane" ref={desiredRef} onScroll={bindScroll(desiredRef, liveRef)}>
            {groups.map((g, i) => renderColumn(g, i, "desired"))}
          </pre>
        </div>
      </div>
    </section>
  );
}

type Group =
  | { kind: "chunk"; chunk: Chunk; liveLine: number | null; desiredLine: number | null }
  | { kind: "fold"; count: number };

function collapseEqual(chunks: Chunk[], hide: boolean): Group[] {
  const out: Group[] = [];
  let liveLine = 0;
  let desiredLine = 0;
  let run: { chunk: Chunk; liveLine: number | null; desiredLine: number | null }[] = [];

  const flush = () => {
    if (!run.length) return;
    // Keep 2 lines of context before and after; fold the rest.
    if (hide && run.length > 6) {
      const head = run.slice(0, 2);
      const tail = run.slice(-2);
      head.forEach((h) => out.push({ kind: "chunk", ...h }));
      out.push({ kind: "fold", count: run.length - 4 });
      tail.forEach((t) => out.push({ kind: "chunk", ...t }));
    } else {
      run.forEach((r) => out.push({ kind: "chunk", ...r }));
    }
    run = [];
  };

  for (const c of chunks) {
    const l = c.type !== "add" ? ++liveLine : null;
    const d = c.type !== "del" ? ++desiredLine : null;
    if (c.type === "eq") {
      run.push({ chunk: c, liveLine: l, desiredLine: d });
    } else {
      flush();
      out.push({ kind: "chunk", chunk: c, liveLine: l, desiredLine: d });
    }
  }
  flush();
  return out;
}

function renderColumn(g: Group, key: number, side: "live" | "desired"): JSX.Element {
  if (g.kind === "fold") {
    return (
      <div key={key} className="diff-fold" title={`${g.count} unchanged lines hidden`}>
        ⋯ {g.count} unchanged
      </div>
    );
  }
  const { chunk, liveLine, desiredLine } = g;
  const isLive = side === "live";
  const showing = isLive
    ? chunk.type === "add" ? null : chunk.live
    : chunk.type === "del" ? null : chunk.desired;
  if (showing === null) {
    return <div key={key} className="diff-placeholder"><span className="diff-ln"> </span> </div>;
  }
  const cls = chunk.type === "eq"
    ? ""
    : isLive
      ? (chunk.type === "del" ? "diff-removed" : "diff-placeholder")
      : (chunk.type === "add" ? "diff-added" : "diff-placeholder");
  const ln = isLive ? liveLine : desiredLine;
  return (
    <div key={key} className={cls}>
      <span className="diff-ln">{ln ?? " "}</span>
      {renderLine(showing)}
    </div>
  );
}

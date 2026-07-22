import { useEffect, useMemo, useState } from "react";
import { fetchDiff } from "../api";
import type { DiffDocument } from "../types";

type Chunk = { type: "eq" | "del" | "add"; live?: string; desired?: string };

// Longest Common Subsequence over line arrays — space-efficient enough for our
// side-by-side view (backend payload is normalized JSON, typically <2000 lines).
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

export function DiffTab({ appName }: { appName: string }) {
  const [doc, setDoc] = useState<DiffDocument | null>(null);
  const [error, setError] = useState<string | null>(null);

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
    const live = JSON.stringify(doc.live, null, 2).split("\n");
    const desired = JSON.stringify(doc.desired, null, 2).split("\n");
    return diffLines(live, desired);
  }, [doc]);

  if (error) return <div className="banner error">{error}</div>;
  if (!doc) return <div className="muted">Computing diff…</div>;

  return (
    <section>
      <div className="diff-cols">
        <div className="diff-col">
          <h3>Live (AWS)</h3>
          <pre className="manifest diff-pane">
            {chunks.map((c, i) =>
              c.type === "add" ? (
                <div key={i} className="diff-placeholder">{" "}</div>
              ) : (
                <div key={i} className={c.type === "del" ? "diff-removed" : ""}>
                  {c.live || " "}
                </div>
              )
            )}
          </pre>
        </div>
        <div className="diff-col">
          <h3>Desired (Git)</h3>
          <pre className="manifest diff-pane">
            {chunks.map((c, i) =>
              c.type === "del" ? (
                <div key={i} className="diff-placeholder">{" "}</div>
              ) : (
                <div key={i} className={c.type === "add" ? "diff-added" : ""}>
                  {c.desired || " "}
                </div>
              )
            )}
          </pre>
        </div>
      </div>
    </section>
  );
}

import type { HistoryEntry } from "../types";

function formatDuration(ms: number): string {
  if (!ms) return "";
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

// Show just the tag/digest tail of an image so long ECR URLs stay readable.
function shortImage(image: string): string {
  const name = image.split("/").pop() ?? image;
  return name.length > 40 ? name.slice(0, 39) + "…" : name;
}

const OUTCOME: Record<string, { cls: string; icon: string; label: string }> = {
  Succeeded: { cls: "ok", icon: "✓", label: "Synced" },
  Error: { cls: "err", icon: "✗", label: "Failed" },
  DryRun: { cls: "dry", icon: "◌", label: "Dry-run" },
};

export function Timeline({ entries }: { entries: HistoryEntry[] }) {
  if (entries.length === 0) {
    return <div className="muted">No deployments recorded yet.</div>;
  }
  return (
    <div className="timeline">
      {entries.map((h) => {
        const o = OUTCOME[h.status] ?? OUTCOME.Succeeded;
        return (
          <div key={h.id} className="timeline-item">
            <span className={`timeline-dot ${o.cls}`} />
            <div className="timeline-body">
              <div className="timeline-head">
                <span className={`timeline-status ${o.cls}`}>{o.icon} {o.label}</span>
                {h.commit && <span className="commit">{h.commit.slice(0, 8)}</span>}
                {h.durationMs > 0 && (
                  <span className="timeline-badge" title="duration">{formatDuration(h.durationMs)}</span>
                )}
                <span className="muted" title={h.createdAt ? new Date(h.createdAt).toLocaleString() : ""}>
                  {relativeTime(h.createdAt)}
                </span>
              </div>
              {h.images.length > 0 && (
                <div className="timeline-images">
                  {h.images.map((img, i) => (
                    <span key={i} className="label-chip" title={img}>{shortImage(img)}</span>
                  ))}
                </div>
              )}
              {h.actions.map((a, i) => (
                <div key={i} className="change ok">• {a}</div>
              ))}
              {h.status === "Error" && h.message && (
                <div className="change err">• {h.message}</div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

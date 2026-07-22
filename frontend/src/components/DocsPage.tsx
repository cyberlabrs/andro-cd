import { useEffect, useMemo, useState } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";

interface Page {
  slug: string;
  title: string;
}

interface Heading {
  id: string;
  text: string;
  level: number;
}

marked.setOptions({ gfm: true, breaks: false });

const slugify = (text: string) =>
  text.toLowerCase().replace(/[^\w\s-]/g, "").trim().replace(/\s+/g, "-");

export function DocsPage({ onClose }: { onClose: () => void }) {
  const [pages, setPages] = useState<Page[]>([]);
  const [slug, setSlug] = useState<string>("index");
  const [markdown, setMarkdown] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    fetch("/api/docs")
      .then((r) => r.json())
      .then((d) => setPages(d.pages ?? []))
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    setMarkdown("");
    fetch(`/api/docs/${slug}.md`)
      .then((r) => (r.ok ? r.text() : Promise.reject(`status ${r.status}`)))
      .then(setMarkdown)
      .catch((e) => setError(String(e)));
  }, [slug]);

  const { html, headings } = useMemo(() => {
    if (!markdown) return { html: "", headings: [] as Heading[] };
    const headings: Heading[] = [];
    const renderer = new marked.Renderer();
    renderer.heading = (token: any) => {
      const text = token.text ?? token;
      const level = token.depth ?? 2;
      const id = slugify(text);
      if (level <= 3) headings.push({ id, text, level });
      return `<h${level} id="${id}">${text}</h${level}>`;
    };
    const rendered = marked.parse(markdown, { renderer }) as string;
    // Sanitize HTML so future user-supplied docs (or a compromised repo) can't inject scripts.
    return { html: DOMPurify.sanitize(rendered), headings };
  }, [markdown]);

  const filteredHeadings = query
    ? headings.filter((h) => h.text.toLowerCase().includes(query.toLowerCase()))
    : headings;

  return (
    <div className="docs-page">
      <header className="header">
        <div className="brand">
          <span className="logo">⬢</span>
          <h1>Andro-CD Docs</h1>
        </div>
        <div className="header-right">
          <button className="btn" onClick={onClose}>← Back to dashboard</button>
        </div>
      </header>

      <div className="docs-layout">
        <aside className="docs-sidebar">
          {pages.length > 1 && (
            <>
              <div className="docs-sidebar-heading">Documents</div>
              {pages.map((p) => (
                <button
                  key={p.slug}
                  className={`docs-page-link ${slug === p.slug ? "active" : ""}`}
                  onClick={() => setSlug(p.slug)}
                >
                  {p.title}
                </button>
              ))}
            </>
          )}

          <div className="docs-sidebar-heading" style={{ marginTop: 16 }}>On this page</div>
          <input
            className="search"
            placeholder="Filter sections…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ width: "100%", marginBottom: 10 }}
          />
          <nav className="docs-toc">
            {filteredHeadings.map((h) => (
              <a
                key={h.id}
                href={`#${h.id}`}
                className={`docs-toc-item docs-toc-h${h.level}`}
                onClick={(e) => {
                  e.preventDefault();
                  document.getElementById(h.id)?.scrollIntoView({ behavior: "smooth" });
                }}
              >
                {h.text}
              </a>
            ))}
          </nav>
        </aside>

        <main className="docs-content">
          {error && <div className="banner error">{error}</div>}
          {!markdown && !error && <div className="muted">Loading…</div>}
          {html && (
            <article className="docs-article" dangerouslySetInnerHTML={{ __html: html }} />
          )}
        </main>
      </div>
    </div>
  );
}

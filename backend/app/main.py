import asyncio
import hmac as hmac_module
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, db, metrics
from .api import router
from .config import settings
from .engine import load_profiles, load_repos, restore_state, run_loop

if settings.log_format == "json":
    import json as _json

    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            out = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                out["exc"] = self.formatException(record.exc_info)
            return _json.dumps(out)

    _handler = logging.StreamHandler()
    _handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[_handler])
else:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("andro-cd")

# ---------- docs (bundled with the image, publicly served) ----------

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"


def _find_repo_root_md(name: str) -> Path | None:
    """Locate SPEC.md / IMPROVEMENTS.md across dev + container layouts."""
    candidates = [
        Path(__file__).resolve().parents[2] / name,   # backend/app/main.py -> repo root
        Path(__file__).resolve().parents[1].parent / name,
        Path("/srv") / name,                          # docker: root of the container app dir
        Path.cwd() / name,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


REPO_ROOT_MD = {"spec": "SPEC.md", "improvements": "IMPROVEMENTS.md"}
_docs_cache: dict[str, tuple[str, str]] = {}  # slug -> (title, content)


def _load_docs() -> None:
    """Read all docs pages into memory once at startup (small, immutable in-image).
    Fills in SPEC/IMPROVEMENTS from the repo root when the docs/ copies are stale — bug #33."""
    _docs_cache.clear()
    if not DOCS_DIR.is_dir():
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for slug, filename in REPO_ROOT_MD.items():
        root_path = _find_repo_root_md(filename)
        docs_path = DOCS_DIR / f"{slug}.md"
        if root_path is not None:
            try:
                if not docs_path.is_file() or root_path.stat().st_mtime > docs_path.stat().st_mtime:
                    docs_path.write_text(root_path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass  # docs dir may be read-only in some deployments
    for path in sorted(DOCS_DIR.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        title = path.stem
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("# "):
                title = s.lstrip("# ").strip()
                break
        _docs_cache[path.stem] = (title, content)


# ---------- lifespan ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    for problem in settings.startup_problems():
        log.warning("config: %s", problem)
    if await asyncio.to_thread(db.init_db):
        await asyncio.to_thread(restore_state)
    await asyncio.to_thread(load_repos)
    await asyncio.to_thread(load_profiles)
    _load_docs()
    task = asyncio.create_task(run_loop())
    log.info("reconcile loop started (interval=%ss auto_sync=%s) — manage repos via the UI or /api/repos",
             settings.sync_interval, settings.auto_sync)
    yield
    task.cancel()
    try:
        await task    # bug #32 — let the loop exit cleanly on SIGTERM
    except asyncio.CancelledError:
        pass
    await asyncio.to_thread(db.release_leadership)   # hand the HA lock to a standby fast


app = FastAPI(title="Andro-CD", lifespan=lifespan)

# Bug #30: register middleware before routers so it wraps every request.
# Bug #3: use an exact-set membership for auth exemptions instead of prefix matching.
AUTH_EXEMPT_EXACT = {"/api/docs", "/api/schema"}
AUTH_EXEMPT_PREFIXES = ("/api/auth/", "/api/webhook/", "/api/docs/")

# Security headers on every response. The CSP allows only same-origin scripts;
# inline styles are needed by React style props. Avatar image sources depend on the
# auth mode — GitHub's CDN, or (OIDC) an arbitrary provider/CDN over https.
_IMG_SRC = ("'self' data: https:" if settings.auth_mode == "oidc"
            else "'self' data: https://avatars.githubusercontent.com")
_FORM_ACTION = "'self' https://github.com" if settings.auth_mode == "github" else "'self'"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"img-src {_IMG_SRC}; "
        f"connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        f"form-action {_FORM_ACTION}"
    ),
}


def _auth_misconfig() -> str | None:
    """Human-readable reason the configured auth mode can't work, or None if OK."""
    if settings.auth_mode == "github" and not (settings.github_client_id and settings.github_client_secret):
        return "auth misconfigured: GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET missing"
    if settings.auth_mode == "oidc" and not (
        settings.oidc_issuer and settings.oidc_client_id and settings.oidc_client_secret
    ):
        return "auth misconfigured: OIDC_ISSUER / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET missing"
    return None


def _csrf_origin_ok(request: Request) -> bool:
    """Reject state-changing browser requests whose Origin doesn't match this
    deployment (CSRF / DNS-rebinding defence). Non-browser clients (CLI, curl,
    GitHub webhooks) send no Origin header and pass through."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    allowed = {settings.public_url}
    host = request.headers.get("host", "")
    if host:
        allowed.add(f"http://{host}")
        allowed.add(f"https://{host}")
    return origin.rstrip("/") in allowed


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    if (request.method in ("POST", "PUT", "PATCH", "DELETE")
            and path.startswith("/api")
            and not path.startswith("/api/webhook/")   # HMAC-protected, no Origin sent
            and not _csrf_origin_ok(request)):
        return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)

    if settings.auth_enabled:
        exempt = path in AUTH_EXEMPT_EXACT or path.startswith(AUTH_EXEMPT_PREFIXES)
        if path.startswith("/api") and not exempt:
            # Static API tokens (CI/automation): Authorization: Bearer <token>
            header = request.headers.get("authorization", "")
            token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
            token_role = settings.api_tokens.get(token) if token else None
            if token_role:
                request.state.user = {"login": f"api-token:{token[:6]}", "api_role": token_role}
            else:
                misconfig = _auth_misconfig()
                if misconfig:
                    return JSONResponse({"detail": misconfig}, status_code=503)
                session = request.cookies.get(auth.SESSION_COOKIE, "")
                user = auth.verify_session(session)
                if not user:
                    return JSONResponse({"detail": "authentication required"}, status_code=401)
                request.state.user = user

    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if settings.cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return response


app.include_router(router)
app.include_router(auth.router)
# OIDC routes are always mounted but self-guard via _require_configured (404 unless
# AUTH_MODE=oidc), mirroring how the GitHub OAuth routes behave.
from . import oidc  # noqa: E402
app.include_router(oidc.router)


# ---------- public routes ----------

@app.get("/api/docs")
def docs_index():
    """Return the docs table of contents (public)."""
    return {"pages": [{"slug": slug, "title": title}
                      for slug, (title, _) in sorted(_docs_cache.items())]}


@app.get("/api/docs/{slug}.md")
def docs_page(slug: str):
    """Serve a docs page as raw Markdown (public)."""
    if not slug.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "invalid slug")
    entry = _docs_cache.get(slug)
    if not entry:
        raise HTTPException(404, f"docs page '{slug}' not found")
    return PlainTextResponse(entry[1], media_type="text/markdown")


@app.get("/api/schema")
def manifest_schema():
    """JSON Schema of the manifest format (public) — for CI validation of manifest repos."""
    from .models import Manifest
    schema = Manifest.model_json_schema()
    schema["$id"] = f"{settings.public_url}/api/schema"
    return schema


@app.get("/healthz")
def healthz():
    """Liveness: process is up. Use /readyz for dependency health."""
    return {"status": "ok", "version": settings.version}


@app.get("/readyz")
def readyz():
    """Readiness: degrades when persistence is down or git polling has stalled."""
    from datetime import datetime, timezone
    from .state import store

    problems: list[str] = []
    if settings.database_url and not db.ready():
        problems.append("database unreachable")
    if store.last_poll:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(store.last_poll)).total_seconds()
        if age > max(settings.sync_interval * 3, 180):
            problems.append(f"git poll stale ({int(age)}s ago)")
    with store.lock():
        failing = [r["url"] for r in store.repos.values() if r.get("error")]
    if failing:
        problems.append(f"{len(failing)} repo(s) failing to sync")
    status_code = 503 if problems else 200
    return JSONResponse({"status": "degraded" if problems else "ok",
                         "problems": problems}, status_code=status_code)


@app.get("/metrics")
def prometheus_metrics(request: Request):
    # Optional bearer token (Prometheus scrape config). If unset — publicly readable.
    if settings.metrics_token:
        header = request.headers.get("authorization", "")
        expected = f"Bearer {settings.metrics_token}"
        if not hmac_module.compare_digest(header, expected):
            return JSONResponse({"detail": "metrics require bearer token"}, status_code=401)
    payload, content_type = metrics.render()
    return Response(payload, media_type=content_type)


# ---------- SPA (fallback catch-all) ----------

if os.path.isdir(settings.static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(settings.static_dir, "assets")), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        # Resolve and contain within the static root — otherwise `..` or encoded
        # traversal sequences in the catch-all could read arbitrary files.
        static_root = os.path.realpath(settings.static_dir)
        file = os.path.realpath(os.path.join(static_root, path))
        if path and file.startswith(static_root + os.sep) and os.path.isfile(file):
            return FileResponse(file)
        return FileResponse(os.path.join(static_root, "index.html"))

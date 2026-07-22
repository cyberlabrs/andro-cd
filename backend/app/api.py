import asyncio
import hashlib
import hmac
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import db, engine, ratelimit, reconciler
from .config import settings
from .state import store

router = APIRouter(prefix="/api")


ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


def current_role(request: Request) -> str:
    if settings.auth_mode != "github":
        return "admin"
    from . import auth as auth_module
    user = getattr(request.state, "user", None) or {}
    if user.get("api_role"):          # static API token (CI/automation)
        return user["api_role"]
    return auth_module.role_for(user.get("login", "")) if user.get("login") else "viewer"


def require_role(request: Request, minimum: str) -> None:
    if settings.auth_mode != "github":
        return
    role = current_role(request)
    if ROLE_RANK.get(role, 0) < ROLE_RANK[minimum]:
        raise HTTPException(403, f"requires '{minimum}' role (you have '{role}')")


def audit(request: Request, action: str, target: str = "", detail: str = "") -> None:
    """Record who did what (fire-and-forget; auth off => 'anonymous')."""
    user = getattr(request.state, "user", None) or {}
    db.record_audit(
        user=user.get("login") or "anonymous",
        role=current_role(request),
        action=action, target=target, detail=detail[:2000],
        source_ip=ratelimit.client_ip(request),
    )


class RepoIn(BaseModel):
    url: str
    branch: str = "main"
    path: str = ""
    authType: str = "https"        # https | ssh | github_app
    token: str = ""
    sshKey: str = ""
    githubAppId: str = ""
    githubInstallationId: str = ""
    githubPrivateKey: str = ""


@router.get("/status")
def status():
    out = store.status()
    out["dryRun"] = settings.dry_run
    out["leader"] = engine.is_leader()
    out["version"] = settings.version
    return out


@router.get("/repos")
def list_repos():
    with store.lock():
        return [store.repo_public(r) for r in store.repos.values()]


@router.post("/repos", status_code=201)
async def create_repo(repo: RepoIn, request: Request):
    require_role(request, "admin")
    url = repo.url.strip()
    if not url.startswith(("https://", "http://", "git@", "ssh://", "file://")):
        raise HTTPException(400, "unsupported repo URL (use https://, git@, ssh:// or file://)")
    if repo.authType not in ("https", "ssh", "github_app"):
        raise HTTPException(400, "authType must be https, ssh or github_app")
    if repo.authType == "ssh" and not repo.sshKey.strip():
        raise HTTPException(400, "sshKey is required for ssh auth")
    if repo.authType == "github_app" and not (
        repo.githubAppId.strip() and repo.githubInstallationId.strip() and repo.githubPrivateKey.strip()
    ):
        raise HTTPException(400, "githubAppId, githubInstallationId and githubPrivateKey are required for github_app auth")
    if repo.authType == "github_app" and not url.startswith("https://"):
        raise HTTPException(400, "github_app auth requires an https:// repo URL")
    with store.lock():
        duplicate = any(
            r["url"] == url
            and (r.get("branch") or "main") == repo.branch
            and (r.get("path") or "") == repo.path
            for r in store.repos.values()
        )
    if duplicate:
        raise HTTPException(409, "repo with the same url/branch/path already added")
    created = engine.add_repo({
        "url": url,
        "branch": repo.branch.strip() or "main",
        "path": repo.path.strip("/ "),
        "token": repo.token.strip(),
        "auth_type": repo.authType,
        "ssh_key": repo.sshKey.strip(),
        "github_app_id": repo.githubAppId.strip(),
        "github_installation_id": repo.githubInstallationId.strip(),
        "github_private_key": repo.githubPrivateKey.strip(),
    })
    audit(request, "repo.add", url, f"branch={repo.branch} path={repo.path} auth={repo.authType}")
    asyncio.get_running_loop().run_in_executor(None, engine.reconcile_once)
    return created


@router.delete("/repos/{repo_id}")
def delete_repo(repo_id: int, request: Request):
    require_role(request, "admin")
    with store.lock():
        url = (store.repos.get(repo_id) or {}).get("url", "")
    if not engine.remove_repo(repo_id):
        raise HTTPException(404, f"repo {repo_id} not found")
    audit(request, "repo.delete", url or str(repo_id))
    return {"deleted": repo_id}


class ProfileIn(BaseModel):
    name: str
    region: str = ""
    accessKeyId: str
    secretAccessKey: str


@router.get("/profiles")
def list_profiles():
    with store.lock():
        return sorted(
            (store.profile_public(p) for p in store.profiles.values()),
            key=lambda p: p["name"],
        )


@router.post("/profiles", status_code=201)
async def create_profile(profile: ProfileIn, request: Request):
    require_role(request, "admin")
    name = profile.name.strip()
    if not name or "/" in name:
        raise HTTPException(400, "invalid profile name")
    with store.lock():
        if name in store.profiles:
            raise HTTPException(409, f"profile '{name}' already exists")
    try:
        created = await asyncio.to_thread(
            engine.add_profile, name, profile.region.strip(),
            profile.accessKeyId.strip(), profile.secretAccessKey.strip(),
        )
    except Exception as e:
        raise HTTPException(400, f"credentials validation failed: {str(e)[:300]}")
    audit(request, "profile.add", name, f"region={profile.region} account={created.get('accountId', '')}")
    return created


@router.delete("/profiles/{name}")
def delete_profile(name: str, request: Request):
    require_role(request, "admin")
    with store.lock():
        in_use = [a.name for a in store.apps.values()
                  if a.manifest and a.manifest.spec.awsProfile == name]
    if in_use:
        raise HTTPException(409, f"profile is used by: {', '.join(in_use[:5])}")
    if not engine.remove_profile(name):
        raise HTTPException(404, f"profile '{name}' not found")
    audit(request, "profile.delete", name)
    return {"deleted": name}


@router.get("/apps")
def list_apps():
    with store.lock():
        return sorted((a.summary() for a in store.apps.values()), key=lambda a: a["name"])


@router.get("/apps/{name}")
def get_app(name: str):
    with store.lock():
        app = store.apps.get(name)
        if not app:
            raise HTTPException(404, f"app '{name}' not found")
        return app.detail()


def _get_manifest(name: str):
    with store.lock():
        app = store.apps.get(name)
        if not app or not app.manifest:
            raise HTTPException(404, f"app '{name}' not found")
        return app.manifest


@router.get("/apps/{name}/resources")
async def app_resources(name: str):
    manifest = _get_manifest(name)
    try:
        return jsonable_encoder(await asyncio.to_thread(reconciler.get_resources, manifest))
    except Exception as e:
        return {"error": str(e)[:500], "cluster": None, "service": None,
                "taskDefinition": None, "tasks": []}


STREAM_POLL_SECONDS = 2.5
STREAM_MAX_SECONDS = 30 * 60


@router.get("/apps/{name}/logs/stream")
async def stream_logs(name: str, request: Request, container: str | None = None):
    """Server-Sent Events: pushes new CloudWatch log events as they appear."""
    manifest = _get_manifest(name)

    async def gen():
        # Bug #23: start with the last 2 minutes only. Loading 10 minutes of history for
        # busy services flooded the stream and older events were silently truncated.
        cursor = int((time.time() - 120) * 1000)
        seen: dict[str, None] = {}
        started = time.monotonic()
        while time.monotonic() - started < STREAM_MAX_SECONDS:
            if await request.is_disconnected():
                return
            try:
                batch = await asyncio.to_thread(
                    reconciler.log_events_since, manifest, container, cursor
                )
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)[:400]})}\n\n"
                return
            if batch.get("error"):
                yield f"event: error\ndata: {json.dumps({'error': batch['error']})}\n\n"
                return

            fresh = [e for e in batch.get("events", []) if e["id"] not in seen]
            for e in fresh:
                seen[e["id"]] = None
                cursor = max(cursor, e["ts"])
                yield f"data: {json.dumps({'timestamp': e['timestamp'], 'message': e['message']})}\n\n"
            # bound the dedupe set
            while len(seen) > 5000:
                seen.pop(next(iter(seen)))

            yield f"event: meta\ndata: {json.dumps({'container': batch.get('container'), 'containers': batch.get('containers', []), 'group': batch.get('group')})}\n\n"
            await asyncio.sleep(STREAM_POLL_SECONDS)
        yield f"event: error\ndata: {json.dumps({'error': 'stream timeout — reconnect to continue'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@router.get("/apps/{name}/logs")
async def app_logs(name: str, container: str | None = None, lines: int = 100):
    manifest = _get_manifest(name)
    try:
        return await asyncio.to_thread(reconciler.get_logs, manifest, container, min(lines, 500))
    except Exception as e:
        return {"containers": [], "error": str(e)[:500]}


@router.get("/apps/{name}/history")
async def app_history(name: str, limit: int = 20):
    return await asyncio.to_thread(db.get_history, name, min(limit, 100))


@router.post("/apps/{name}/sync")
async def sync_app(name: str, request: Request):
    require_role(request, "operator")
    audit(request, "app.sync", name)
    try:
        return await asyncio.to_thread(engine.sync_single, name)
    except KeyError:
        raise HTTPException(404, f"app '{name}' not found")


@router.get("/apps/{name}/diff")
async def app_diff(name: str):
    manifest = _get_manifest(name)
    try:
        return jsonable_encoder(await asyncio.to_thread(reconciler.get_diff_document, manifest))
    except Exception as e:
        return {"error": str(e)[:500], "desired": None, "live": None}


@router.get("/apps/{name}/revisions")
async def app_revisions(name: str):
    manifest = _get_manifest(name)
    try:
        return jsonable_encoder(await asyncio.to_thread(reconciler.list_revisions, manifest))
    except Exception as e:
        raise HTTPException(502, str(e)[:400])


class RollbackIn(BaseModel):
    revision: int


@router.post("/apps/{name}/rollback")
async def rollback_app(name: str, body: RollbackIn, request: Request):
    require_role(request, "operator")
    audit(request, "app.rollback", name, f"revision={body.revision}")
    try:
        return await asyncio.to_thread(engine.rollback_single, name, body.revision)
    except KeyError:
        raise HTTPException(404, f"app '{name}' not found")
    except Exception as e:
        raise HTTPException(502, str(e)[:400])


@router.post("/apps/{name}/prune")
async def prune_app(name: str, request: Request):
    require_role(request, "operator")
    audit(request, "app.prune", name)
    try:
        return await asyncio.to_thread(engine.prune_single, name)
    except KeyError:
        raise HTTPException(404, f"app '{name}' not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/refresh")
async def refresh(request: Request):
    require_role(request, "operator")
    audit(request, "refresh")
    await asyncio.to_thread(engine.reconcile_once)
    return store.status()


@router.get("/audit")
def audit_log(request: Request, limit: int = 100,
              user: str | None = None, action: str | None = None):
    """Audit trail: who synced/rolled back/pruned what and when (admin only)."""
    require_role(request, "admin")
    return db.list_audit(min(max(limit, 1), 500), user, action)


WEBHOOK_MAX_BODY = 1024 * 1024   # 1 MiB — real GitHub push payloads are far smaller


@router.post("/webhook/github")
async def github_webhook(request: Request):
    if not settings.webhook_secret:
        raise HTTPException(503, "webhook disabled: WEBHOOK_SECRET not configured")
    if not ratelimit.allow(f"webhook:{ratelimit.client_ip(request)}", limit=60):
        raise HTTPException(429, "too many webhook requests")
    body = await request.body()
    if len(body) > WEBHOOK_MAX_BODY:
        raise HTTPException(413, "payload too large")
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"ok": True}
    if event != "push":
        return {"triggered": False, "reason": f"ignored event '{event}'"}

    # GitHub sends form-urlencoded when the webhook content-type is left at default —
    # unwrap the payload={...} form field so both configurations work (bug #35).
    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("application/x-www-form-urlencoded"):
            from urllib.parse import parse_qs
            form = parse_qs(body.decode())
            payload = json.loads(form.get("payload", ["{}"])[0])
        else:
            payload = json.loads(body)
    except (ValueError, KeyError, IndexError):
        raise HTTPException(400, "webhook body is not a valid GitHub payload")

    ref = payload.get("ref", "")
    repo_info = payload.get("repository") or {}
    pushed_urls = {
        _norm_url(repo_info.get("clone_url", "")),
        _norm_url(repo_info.get("html_url", "")),
        _norm_url(repo_info.get("ssh_url", "")),
    }
    with store.lock():
        tracked = list(store.repos.values())
    match = any(
        _norm_url(r["url"]) in pushed_urls and ref == f"refs/heads/{r.get('branch') or 'main'}"
        for r in tracked
    )
    if not match:
        return {"triggered": False, "reason": f"no tracked repo matches ref '{ref}'"}

    asyncio.get_running_loop().run_in_executor(None, engine.reconcile_once)
    return {"triggered": True}


def _norm_url(url: str) -> str:
    return url.rstrip("/").removesuffix(".git").lower()

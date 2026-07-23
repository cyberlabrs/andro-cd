import base64
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import ratelimit
from .config import settings

log = logging.getLogger("andro-cd.auth")

router = APIRouter(prefix="/api/auth")

SESSION_COOKIE = "androcd_session"
STATE_COOKIE = "androcd_oauth_state"
SESSION_TTL = 8 * 3600

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"


# ---------- signed sessions ----------

def _sign(payload: str) -> str:
    import hashlib
    return hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session(user: dict) -> str:
    data = {**user, "exp": int(time.time()) + SESSION_TTL}
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload)}"


def verify_session(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


# ---------- GitHub API ----------

def _github_json(url: str, data: Optional[dict] = None, token: Optional[str] = None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _redirect_uri() -> str:
    return f"{settings.public_url}/api/auth/callback"


def role_for(login: str) -> str:
    """RBAC: admin > operator > viewer. Without any RBAC config everyone is admin."""
    login = login.lower()
    if login in settings.rbac_admins:
        return "admin"
    if login in settings.rbac_operators:
        return "operator"
    if settings.rbac_default_role in ("admin", "operator", "viewer"):
        return settings.rbac_default_role
    return "viewer" if (settings.rbac_admins or settings.rbac_operators) else "admin"


def _require_configured() -> None:
    if settings.auth_mode != "github":
        raise HTTPException(404, "authentication is disabled (AUTH_MODE=none)")
    if not settings.github_client_id or not settings.github_client_secret:
        raise HTTPException(503, "auth misconfigured: GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET missing")


# ---------- routes ----------

@router.get("/login")
def login(request: Request):
    _require_configured()
    if not ratelimit.allow(f"login:{ratelimit.client_ip(request)}", limit=20):
        raise HTTPException(429, "too many login attempts — try again in a minute")
    state = secrets.token_urlsafe(24)
    scope = "read:user" + (" read:org" if settings.github_allowed_org else "")
    params = urllib.parse.urlencode({
        "client_id": settings.github_client_id,
        "redirect_uri": _redirect_uri(),
        "scope": scope,
        "state": state,
    })
    resp = RedirectResponse(f"{GITHUB_AUTHORIZE}?{params}")
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                    samesite="lax", secure=settings.cookie_secure)
    return resp


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = ""):
    _require_configured()
    if not ratelimit.allow(f"callback:{ratelimit.client_ip(request)}", limit=20):
        raise HTTPException(429, "too many login attempts — try again in a minute")
    if not code or not state or state != request.cookies.get(STATE_COOKIE):
        raise HTTPException(400, "invalid OAuth state")

    try:
        token_resp = _github_json(GITHUB_TOKEN, data={
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
            "redirect_uri": _redirect_uri(),
        })
    except urllib.error.URLError as e:
        raise HTTPException(502, f"GitHub token exchange failed: {e}")
    access_token = token_resp.get("access_token")
    if not access_token:
        raise HTTPException(401, f"GitHub auth failed: {token_resp.get('error_description', 'no token')}")

    user = _github_json(f"{GITHUB_API}/user", token=access_token)
    login_name = (user.get("login") or "").lower()
    if not login_name:
        raise HTTPException(401, "could not read GitHub user")

    if settings.github_allowed_users and login_name not in settings.github_allowed_users:
        log.warning("login denied for '%s': not in GITHUB_ALLOWED_USERS", login_name)
        raise HTTPException(403, f"user '{login_name}' is not allowed")

    if settings.github_allowed_org:
        try:
            membership = _github_json(
                f"{GITHUB_API}/user/memberships/orgs/{settings.github_allowed_org}",
                token=access_token,
            )
            if membership.get("state") != "active":
                raise HTTPException(403, f"membership in '{settings.github_allowed_org}' is not active")
        except urllib.error.HTTPError:
            log.warning("login denied for '%s': not a member of org '%s'",
                        login_name, settings.github_allowed_org)
            raise HTTPException(403, f"user '{login_name}' is not a member of '{settings.github_allowed_org}'")

    # Role is NOT baked into the session — it's computed on every request from the
    # current RBAC env vars, so admins can change roles without forcing a re-login.
    session = create_session({
        "login": user["login"],
        "name": user.get("name"),
        "avatar": user.get("avatar_url"),
    })
    log.info("user '%s' logged in", login_name)
    from . import db
    db.record_audit(login_name, role_for(login_name), "auth.login",
                    source_ip=ratelimit.client_ip(request))
    resp = RedirectResponse("/")
    resp.set_cookie(SESSION_COOKIE, session, max_age=SESSION_TTL, httponly=True,
                    samesite="lax", secure=settings.cookie_secure)
    resp.delete_cookie(STATE_COOKIE)
    return resp


@router.get("/me")
def me(request: Request):
    if not settings.auth_enabled:
        return {"mode": "none", "authenticated": True, "user": None, "role": "admin"}
    mode = settings.auth_mode   # "github" | "oidc"
    user = verify_session(request.cookies.get(SESSION_COOKIE, ""))
    if user:
        role = role_for(user["login"])   # always recomputed from current config
        return {"mode": mode, "authenticated": True, "role": role,
                "user": {"login": user["login"], "name": user.get("name"),
                         "avatar": user.get("avatar"), "role": role}}
    return {"mode": mode, "authenticated": False, "user": None, "role": None}


@router.post("/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp

"""Generic OpenID Connect login (AUTH_MODE=oidc).

Works with any spec-compliant provider — Google, Okta, Keycloak, Dex, Auth0, Azure AD —
via the discovery document (`/.well-known/openid-configuration`). Uses the authorization
code flow with PKCE, a nonce, and full id-token verification against the provider's JWKS.

Sessions reuse the same signed-cookie mechanism as the GitHub flow (see auth.py), so the
middleware, RBAC and /me endpoint are auth-mode agnostic.
"""
import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import auth, ratelimit
from .config import settings

log = logging.getLogger("androcd.oidc")

router = APIRouter(prefix="/api/auth/oidc")

STATE_COOKIE = "androcd_oidc_flow"
FLOW_TTL = 600  # seconds the login round-trip may take

# Signature algorithms we accept for the id token (asymmetric only — never "none"/HS).
_ALLOWED_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]

_discovery_cache: dict[str, tuple[dict, float]] = {}
_jwk_clients: dict[str, "jwt.PyJWKClient"] = {}


# ---------- provider discovery ----------

def _discover() -> dict:
    """Fetch and cache the provider's OpenID configuration (1h TTL)."""
    now = time.time()
    cached = _discovery_cache.get(settings.oidc_issuer)
    if cached and cached[1] > now:
        return cached[0]
    url = settings.oidc_issuer + "/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=10) as resp:
        cfg = json.loads(resp.read())
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if required not in cfg:
            raise HTTPException(502, f"OIDC discovery missing '{required}' at {url}")
    _discovery_cache[settings.oidc_issuer] = (cfg, now + 3600)
    return cfg


def _jwk_client(jwks_uri: str) -> "jwt.PyJWKClient":
    client = _jwk_clients.get(jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(jwks_uri)
        _jwk_clients[jwks_uri] = client
    return client


def _require_configured() -> None:
    if settings.auth_mode != "oidc":
        raise HTTPException(404, "OIDC authentication is disabled (AUTH_MODE is not 'oidc')")
    if not (settings.oidc_issuer and settings.oidc_client_id and settings.oidc_client_secret):
        raise HTTPException(503, "auth misconfigured: OIDC_ISSUER / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET missing")


def _redirect_uri() -> str:
    return f"{settings.public_url}/api/auth/oidc/callback"


# ---------- signed flow cookie (state + nonce + PKCE verifier) ----------

def _pack_flow(data: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{payload}.{auth._sign(payload)}"


def _unpack_flow(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    import hmac
    if not hmac.compare_digest(signature, auth._sign(payload)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


# ---------- HTTP helpers ----------

def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ---------- access control ----------

def _extract_groups(claims: dict) -> list[str]:
    groups = claims.get(settings.oidc_groups_claim)
    if isinstance(groups, str):
        return [groups]
    if isinstance(groups, list):
        return [str(g) for g in groups]
    return []


def _check_allowed(username: str, claims: dict) -> None:
    """Enforce every configured allowlist (fail closed, mirrors the GitHub flow)."""
    login = username.lower()
    if settings.oidc_allowed_users and login not in settings.oidc_allowed_users:
        raise HTTPException(403, f"user '{username}' is not allowed")
    if settings.oidc_allowed_domains:
        domain = login.split("@")[-1] if "@" in login else ""
        if domain not in settings.oidc_allowed_domains:
            raise HTTPException(403, f"email domain of '{username}' is not allowed")
    if settings.oidc_allowed_groups:
        groups = set(_extract_groups(claims))
        if not (groups & settings.oidc_allowed_groups):
            raise HTTPException(403, f"user '{username}' is not in an allowed group")


# ---------- routes ----------

@router.get("/login")
def login(request: Request):
    _require_configured()
    if not ratelimit.allow(f"oidc-login:{ratelimit.client_ip(request)}", limit=20):
        raise HTTPException(429, "too many login attempts — try again in a minute")
    cfg = _discover()

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")

    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": _redirect_uri(),
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    resp = RedirectResponse(f"{cfg['authorization_endpoint']}?{params}")
    flow = _pack_flow({"state": state, "nonce": nonce, "cv": verifier,
                       "exp": int(time.time()) + FLOW_TTL})
    resp.set_cookie(STATE_COOKIE, flow, max_age=FLOW_TTL, httponly=True,
                    samesite="lax", secure=settings.cookie_secure)
    return resp


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = ""):
    _require_configured()
    if not ratelimit.allow(f"oidc-callback:{ratelimit.client_ip(request)}", limit=20):
        raise HTTPException(429, "too many login attempts — try again in a minute")

    flow = _unpack_flow(request.cookies.get(STATE_COOKIE, ""))
    if not flow or not code or not state or not secrets.compare_digest(state, flow["state"]):
        raise HTTPException(400, "invalid OAuth state")

    cfg = _discover()
    try:
        tokens = _post_form(cfg["token_endpoint"], {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
            "code_verifier": flow["cv"],
        })
    except urllib.error.HTTPError as e:
        raise HTTPException(401, f"OIDC token exchange failed: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"OIDC token endpoint unreachable: {e}")

    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(401, "OIDC provider returned no id_token")

    # Verify the id token against the provider's JWKS: signature, issuer, audience, expiry.
    try:
        signing_key = _jwk_client(cfg["jwks_uri"]).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, signing_key.key,
            algorithms=cfg.get("id_token_signing_alg_values_supported") or _ALLOWED_ALGS,
            audience=settings.oidc_client_id,
            issuer=cfg["issuer"],
            options={"require": ["exp", "iat", "aud"]},
        )
    except Exception as e:
        log.warning("id_token verification failed: %s", e)
        raise HTTPException(401, "id_token verification failed")

    if claims.get("nonce") != flow["nonce"]:
        raise HTTPException(401, "OIDC nonce mismatch")

    # Groups may live only in userinfo for some providers — merge them in when needed.
    if settings.oidc_allowed_groups and settings.oidc_groups_claim not in claims:
        access_token = tokens.get("access_token")
        userinfo_ep = cfg.get("userinfo_endpoint")
        if access_token and userinfo_ep:
            try:
                claims = {**claims, **_get_json(userinfo_ep, access_token)}
            except Exception as e:
                log.warning("userinfo fetch failed: %s", e)

    username = (claims.get(settings.oidc_username_claim)
                or claims.get("email") or claims.get("preferred_username")
                or claims.get("sub"))
    if not username:
        raise HTTPException(401, f"OIDC id_token has no '{settings.oidc_username_claim}' claim")

    _check_allowed(str(username), claims)

    session = auth.create_session({
        "login": str(username),
        "name": claims.get("name") or claims.get("given_name"),
        "avatar": claims.get("picture"),
    })
    log.info("user '%s' logged in via OIDC", username)
    from . import db
    db.record_audit(str(username).lower(), auth.role_for(str(username)), "auth.login",
                    detail="oidc", source_ip=ratelimit.client_ip(request))

    resp = RedirectResponse("/")
    resp.set_cookie(auth.SESSION_COOKIE, session, max_age=auth.SESSION_TTL, httponly=True,
                    samesite="lax", secure=settings.cookie_secure)
    resp.delete_cookie(STATE_COOKIE)
    return resp

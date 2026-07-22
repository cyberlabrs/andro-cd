import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import jwt

log = logging.getLogger("andro-cd.github-app")

# (app_id, installation_id) -> (token, expires_epoch)
_cache: dict[tuple[str, str], tuple[str, float]] = {}


class GitHubAppError(Exception):
    pass


def installation_token(app_id: str, installation_id: str, private_key_pem: str) -> str:
    """Exchange a GitHub App JWT for a short-lived installation access token (cached)."""
    key = (app_id, installation_id)
    cached = _cache.get(key)
    if cached and cached[1] - time.time() > 300:
        return cached[0]

    now = int(time.time())
    app_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id},
        private_key_pem,
        algorithm="RS256",
    )
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        },
    )
    # Retry once on transient 5xx (GitHub occasionally throws 502/503 briefly).
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            if 500 <= e.code < 600 and attempt == 0:
                log.warning("GitHub App token exchange returned %d, retrying", e.code)
                time.sleep(1)
                last_err = e
                continue
            raise GitHubAppError(f"GitHub App token exchange failed ({e.code}): {body}")
        except urllib.error.URLError as e:
            if attempt == 0:
                log.warning("GitHub App token exchange transport error: %s, retrying", e)
                time.sleep(1)
                last_err = e
                continue
            raise GitHubAppError(f"GitHub App token exchange failed: {e}")
    else:
        raise GitHubAppError(f"GitHub App token exchange failed after retries: {last_err}")

    token = data["token"]
    expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    _cache[key] = (token, expires.astimezone(timezone.utc).timestamp())
    log.info("obtained installation token for app %s (installation %s)", app_id, installation_id)
    return token

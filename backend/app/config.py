import os
import secrets

# empty AWS_* env vars (e.g. from docker-compose passthrough defaults) break boto3 —
# an empty AWS_PROFILE makes it look up a profile named ""
for _var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    if os.environ.get(_var, None) == "":
        del os.environ[_var]


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _read_version() -> str:
    """App version from version.txt (repo root in dev, /srv in the image).
    Maintained by release-please — do not edit by hand."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for candidate in (os.path.join(here, "version.txt"), "/srv/version.txt"):
        try:
            with open(candidate) as f:
                return f.read().strip() or "dev"
        except OSError:
            continue
    return "dev"


class Settings:
    version: str = _read_version()
    git_repo_url: str = os.getenv("GIT_REPO_URL", "")
    git_branch: str = os.getenv("GIT_BRANCH", "main")
    git_path: str = os.getenv("GIT_PATH", "").strip("/")
    git_token: str = os.getenv("GIT_TOKEN", "")
    sync_interval: int = int(os.getenv("SYNC_INTERVAL", "60"))
    auto_sync: bool = _bool("AUTO_SYNC", True)
    # DRY_RUN=true: every sync/rollback/prune records the plan but never calls AWS
    # mutation APIs — for demos, IAM testing and observation-only deployments.
    dry_run: bool = _bool("DRY_RUN", False)
    # Deregister ACTIVE task definition revisions beyond the newest N after each
    # successful sync (0 = keep everything). The in-use revision is never touched.
    keep_taskdef_revisions: int = max(0, int(os.getenv("KEEP_TASKDEF_REVISIONS", "0")))
    log_format: str = os.getenv("LOG_FORMAT", "text").strip().lower()   # text | json
    aws_region: str = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", ""))
    repos_base_dir: str = os.getenv("REPOS_DIR", "/tmp/andro-cd-repos")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:////tmp/andro-cd/andro-cd.db")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    slack_webhook_url: str = os.getenv("SLACK_WEBHOOK_URL", "")
    auth_mode: str = os.getenv("AUTH_MODE", "none").strip().lower()
    github_client_id: str = os.getenv("GITHUB_CLIENT_ID", "")
    github_client_secret: str = os.getenv("GITHUB_CLIENT_SECRET", "")
    github_allowed_org: str = os.getenv("GITHUB_ALLOWED_ORG", "").strip()
    github_allowed_users: frozenset = frozenset(
        u.strip().lower() for u in os.getenv("GITHUB_ALLOWED_USERS", "").split(",") if u.strip()
    )
    # Generic OIDC (AUTH_MODE=oidc) — Google, Okta, Dex, Keycloak, Auth0, …
    oidc_issuer: str = os.getenv("OIDC_ISSUER", "").rstrip("/")
    oidc_client_id: str = os.getenv("OIDC_CLIENT_ID", "")
    oidc_client_secret: str = os.getenv("OIDC_CLIENT_SECRET", "")
    oidc_scopes: str = os.getenv("OIDC_SCOPES", "openid email profile").strip()
    # which id-token/userinfo claim becomes the username (login) used for RBAC
    oidc_username_claim: str = os.getenv("OIDC_USERNAME_CLAIM", "email").strip()
    oidc_groups_claim: str = os.getenv("OIDC_GROUPS_CLAIM", "groups").strip()
    oidc_allowed_users: frozenset = frozenset(
        u.strip().lower() for u in os.getenv("OIDC_ALLOWED_USERS", "").split(",") if u.strip()
    )
    # email-domain allowlist, e.g. "example.com,corp.example.com"
    oidc_allowed_domains: frozenset = frozenset(
        d.strip().lower().lstrip("@") for d in os.getenv("OIDC_ALLOWED_DOMAINS", "").split(",") if d.strip()
    )
    # group allowlist (matched against the groups claim)
    oidc_allowed_groups: frozenset = frozenset(
        g.strip() for g in os.getenv("OIDC_ALLOWED_GROUPS", "").split(",") if g.strip()
    )
    rbac_admins: frozenset = frozenset(
        u.strip().lower() for u in os.getenv("RBAC_ADMINS", "").split(",") if u.strip()
    )
    rbac_operators: frozenset = frozenset(
        u.strip().lower() for u in os.getenv("RBAC_OPERATORS", "").split(",") if u.strip()
    )
    rbac_default_role: str = os.getenv("RBAC_DEFAULT_ROLE", "").strip().lower()
    # sessions are invalidated on restart unless SESSION_SECRET is set explicitly
    session_secret: str = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
    # encrypts stored AWS profile credentials; falls back to SESSION_SECRET
    encryption_key: str = os.getenv("ENCRYPTION_KEY") or os.getenv("SESSION_SECRET") or session_secret
    public_url: str = os.getenv("PUBLIC_URL", "http://localhost:8080").rstrip("/")
    metrics_token: str = os.getenv("METRICS_TOKEN", "")
    # Static API tokens for CI / automation: "token:role,token2:role2"
    # (role: viewer | operator | admin). Sent as "Authorization: Bearer <token>".
    api_tokens: dict = {
        t.rsplit(":", 1)[0].strip(): t.rsplit(":", 1)[1].strip().lower()
        for t in os.getenv("API_TOKENS", "").split(",")
        if ":" in t and t.rsplit(":", 1)[0].strip()
    }
    # Max parallel diff workers per reconcile pass (perf tuning).
    reconcile_workers: int = max(1, int(os.getenv("RECONCILE_WORKERS", "8")))
    port: int = int(os.getenv("PORT", "8080"))
    static_dir: str = os.getenv("STATIC_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "static"))

    @property
    def cookie_secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def auth_enabled(self) -> bool:
        """True when logins are required (github or oidc), False for AUTH_MODE=none."""
        return self.auth_mode in ("github", "oidc")

    def startup_problems(self) -> list[str]:
        """Config sanity checks — logged as warnings at startup (fail fast on nonsense)."""
        problems: list[str] = []
        if self.auth_mode not in ("none", "github", "oidc"):
            problems.append(f"AUTH_MODE='{self.auth_mode}' is not supported (use 'none', 'github' or 'oidc')")
        if self.auth_mode == "github" and not (self.github_client_id and self.github_client_secret):
            problems.append("AUTH_MODE=github but GITHUB_CLIENT_ID/GITHUB_CLIENT_SECRET are missing — all API requests will fail")
        if self.auth_mode == "oidc" and not (self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret):
            problems.append("AUTH_MODE=oidc but OIDC_ISSUER/OIDC_CLIENT_ID/OIDC_CLIENT_SECRET are missing — all API requests will fail")
        if self.auth_mode in ("github", "oidc") and not os.getenv("SESSION_SECRET"):
            problems.append("SESSION_SECRET not set — sessions and encrypted AWS profiles will not survive restarts")
        if self.auth_mode == "none" and self.public_url.startswith("https://"):
            problems.append("AUTH_MODE=none on a public HTTPS URL — anyone who can reach the UI has full admin access")
        if self.auth_mode == "oidc" and not (
            self.oidc_allowed_users or self.oidc_allowed_domains or self.oidc_allowed_groups
        ):
            problems.append("AUTH_MODE=oidc without OIDC_ALLOWED_USERS/DOMAINS/GROUPS — anyone with an account at the provider can log in")
        if self.sync_interval < 10:
            problems.append(f"SYNC_INTERVAL={self.sync_interval}s is very aggressive — risks AWS/git rate limits")
        if self.dry_run:
            problems.append("DRY_RUN=true — no changes will be applied to AWS (plans only)")
        if self.log_format not in ("text", "json"):
            problems.append(f"LOG_FORMAT='{self.log_format}' is not supported (use 'text' or 'json')")
        for role in self.api_tokens.values():
            if role not in ("viewer", "operator", "admin"):
                problems.append(f"API_TOKENS contains unknown role '{role}' (use viewer/operator/admin)")
        for token in self.api_tokens:
            if len(token) < 16:
                problems.append("API_TOKENS contains a token shorter than 16 chars — use e.g. `openssl rand -hex 32`")
        return problems


settings = Settings()

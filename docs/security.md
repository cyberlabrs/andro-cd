# Security

## Authentication

Two login providers, selected with `AUTH_MODE`. Both protect the UI and every `/api`
route, and both issue the same signed, httpOnly session cookie — set `SESSION_SECRET`
explicitly so sessions survive restarts.

### GitHub OAuth (`AUTH_MODE=github`)

- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` — from a GitHub OAuth App; callback URL is
  `<PUBLIC_URL>/api/auth/callback`.
- `GITHUB_ALLOWED_USERS=alice,bob` and/or `GITHUB_ALLOWED_ORG=my-org` control who may
  log in.

### Generic OIDC (`AUTH_MODE=oidc`)

Works with any spec-compliant provider — Google, Okta, Keycloak, Dex, Auth0, Azure AD —
via the discovery document (`/.well-known/openid-configuration`):

```bash
OIDC_ISSUER=https://accounts.google.com
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_SCOPES="openid email profile"        # add "groups" for group allowlists
OIDC_USERNAME_CLAIM=email                 # claim used as the login for RBAC
# who may log in — each configured allowlist must pass (fail closed):
OIDC_ALLOWED_USERS=alice@example.com,bob@example.com
OIDC_ALLOWED_DOMAINS=example.com
OIDC_ALLOWED_GROUPS=platform,sre
```

- Register the redirect URI `<PUBLIC_URL>/api/auth/oidc/callback` with your provider.
- The flow uses the authorization code grant with **PKCE** and a **nonce**; the id token
  is fully verified against the provider's JWKS (signature, issuer, audience, expiry).
- The username claim becomes the login used everywhere (RBAC, audit log). With no
  allowlist configured, anyone with an account at the provider can log in — a startup
  warning flags this.

## RBAC

| Role | Can do |
|---|---|
| **viewer** | Read everything (apps, resources, logs, history, diff, audit is admin-only) |
| **operator** | + Sync, Rollback, Prune, Refresh |
| **admin** | + Manage repositories, AWS profiles, view the audit log |

```bash
RBAC_ADMINS=alice
RBAC_OPERATORS=bob,carol
RBAC_DEFAULT_ROLE=viewer
```

Values are matched against the login — the GitHub username, or the OIDC username claim
(e.g. `alice@example.com`). Without any RBAC vars, every logged-in user is admin
(single-user convenience). Roles are evaluated on every request — changing the env vars
takes effect without re-login.

## API tokens (CI / automation)

```bash
API_TOKENS=<token>:operator,<token2>:viewer     # generate: openssl rand -hex 32
curl -H "Authorization: Bearer <token>" https://androcd.example/api/apps
```

Each token maps to a role and appears in the audit log as `api-token:<prefix>`.

## Audit log

Every sync, rollback, prune, refresh, repo/profile change and login is persisted with
**user, role, action, target, source IP and timestamp**. Browse it in the **Audit**
panel (admin) or `GET /api/audit?limit=&user=&action=`.

## Hardening (built in)

- **Security headers** on every response: CSP (`default-src 'self'`),
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`; HSTS on https.
- **CSRF protection** — state-changing requests with a mismatched `Origin` are rejected
  (the HMAC-verified webhook is exempt).
- **Rate limiting** on the OAuth flow and webhook; webhook bodies capped at 1 MiB.
- **Git credentials never touch disk** — HTTPS and GitHub App tokens flow through
  in-memory headers; SSH keys are `chmod 600` and deleted with the repo.
- **AWS profile credentials** stored encrypted (Fernet/AES) under `ENCRYPTION_KEY`.
- **Non-root container** (uid 10001) with `no-new-privileges` in compose; Postgres is
  not exposed on the host.
- **Startup config validation** — dangerous combinations (public URL without auth,
  missing secrets, weak tokens) are flagged loudly in the logs at boot.

## Secrets in manifests

`containers[].secrets` maps env var names to SSM / Secrets Manager ARNs — actual values
never pass through Andro-CD and are never rendered in the UI.

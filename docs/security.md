# Security

## Authentication (GitHub OAuth)

`AUTH_MODE=github` protects the UI and every `/api` route:

- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` — from a GitHub OAuth App; callback URL is
  `<PUBLIC_URL>/api/auth/callback`.
- `GITHUB_ALLOWED_USERS=alice,bob` and/or `GITHUB_ALLOWED_ORG=my-org` control who may
  log in.
- Sessions are signed, httpOnly cookies. Set `SESSION_SECRET` explicitly so sessions
  survive restarts.

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

Without any RBAC vars, every logged-in user is admin (single-user convenience).
Roles are evaluated on every request — changing the env vars takes effect without
re-login.

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

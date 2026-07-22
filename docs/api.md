# API & CLI

## HTTP API

All `/api/*` endpoints require authentication when `AUTH_MODE=github` — a session
cookie or an [API token](security.md#api-tokens-ci-automation) — except the OAuth flow,
the HMAC-verified webhook, the docs and `/api/schema`.

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/api/status` | any | Repos, last poll, app count, `leader`, `dryRun`, `version` |
| GET | `/api/apps` | any | Summary of all apps |
| GET | `/api/apps/{name}` | any | Detail: manifest, live state, changes |
| GET | `/api/apps/{name}/diff` | any | Normalized desired vs live JSON |
| GET | `/api/apps/{name}/resources` | any | Live cluster/service/taskdef/tasks |
| GET | `/api/apps/{name}/history` | any | Persisted sync history |
| GET | `/api/apps/{name}/logs` | any | One-shot CloudWatch tail |
| GET | `/api/apps/{name}/logs/stream` | any | Server-Sent Events log stream |
| GET | `/api/apps/{name}/revisions` | any | Recent task-definition revisions |
| POST | `/api/apps/{name}/sync` | operator | Force reconcile |
| POST | `/api/apps/{name}/rollback` | operator | `{revision}` — redeploy, pause auto-sync |
| POST | `/api/apps/{name}/prune` | operator | Delete an Orphaned app's resources |
| POST | `/api/refresh` | operator | Git pull + diff pass now |
| GET | `/api/audit` | admin | Audit trail (`?limit=&user=&action=`) |
| GET | `/api/schema` | — | JSON Schema of the manifest format |
| GET | `/api/repos` | any | List tracked repositories |
| POST | `/api/repos` | admin | Connect a repository |
| DELETE | `/api/repos/{id}` | admin | Disconnect a repository |
| GET | `/api/profiles` | any | List AWS profiles (keys masked) |
| POST | `/api/profiles` | admin | Add a profile (STS-validated, encrypted) |
| DELETE | `/api/profiles/{name}` | admin | Remove a profile |
| POST | `/api/webhook/github` | HMAC | GitHub push webhook |
| GET | `/api/auth/me` | — | Current session + role |
| POST | `/api/auth/logout` | — | Clear the session |
| GET | `/healthz` / `/readyz` | — | Liveness / readiness |
| GET | `/metrics` | — | Prometheus metrics |

## CLI

`backend/cli.py` — for manifest-repo CI and quick operations:

```bash
# Offline validation — values files + ${key} substitution applied exactly
# like the server does it:
python backend/cli.py validate ./manifests

# Against a running server:
export ANDROCD_SERVER=https://androcd.example
export ANDROCD_SESSION=<androcd_session cookie>   # when auth is enabled
python backend/cli.py apps
python backend/cli.py diff my-app
python backend/cli.py sync my-app
```

### Manifest-repo CI example

```yaml
# .github/workflows/validate.yml in your manifest repository
name: Validate manifests
on: [pull_request]
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with: {repository: cyberlabrs/andro-cd, path: andro-cd}
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install pydantic pyyaml
      - run: python andro-cd/backend/cli.py validate ./manifests
```

Invalid YAML or manifests fail the PR before they can ever reach the reconciler.

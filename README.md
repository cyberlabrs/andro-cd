# ⬢ Andro-CD — Pull-based GitOps for AWS ECS

[![CI](https://github.com/cyberlabrs/andro-cd/actions/workflows/ci.yml/badge.svg)](https://github.com/cyberlabrs/andro-cd/actions/workflows/ci.yml)
[![Docs](https://github.com/cyberlabrs/andro-cd/actions/workflows/docs.yml/badge.svg)](https://andro-cd.com/)
[![Release](https://img.shields.io/github/v/release/cyberlabrs/andro-cd?include_prereleases)](https://github.com/cyberlabrs/andro-cd/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Like ArgoCD, but for AWS ECS.** Andro-CD watches Git repositories containing YAML
manifests, diffs them against live AWS state, and reconciles the difference — services,
task definitions, scheduled tasks and the clusters themselves, all declared in Git.

**📖 Documentation: [andro-cd.com](https://andro-cd.com/)**

## Highlights

- **GitOps reconciliation** — poll or webhook-triggered; diff → apply with sync waves,
  pre/post-sync hooks (migrations), self-heal and safe pruning
- **Four kinds** — `ECSService`, `ECSScheduledTask` (EventBridge cron), `ECSServiceSet`
  (app-of-apps generators), `ECSCluster` (insights, capacity providers, Service Connect)
- **Argo-style dashboard** — sync/health per app, side-by-side diff, live CloudWatch
  logs (SSE), stopped-task forensics, one-click sync/rollback/prune, dark mode
- **Production security** — GitHub OAuth + RBAC, API tokens for CI, audit log,
  CSP/CSRF/rate limiting, encrypted multi-account AWS profiles, non-root container
- **Cost-aware** — Fargate Spot strategies, target-tracking autoscaling, labels → AWS
  tags for cost allocation, task-definition cleanup
- **Operations-ready** — HA leader election (Postgres advisory lock), dry-run mode,
  sync windows, values-file templating, Prometheus metrics, Slack notifications

## Quick start

```bash
git clone https://github.com/cyberlabrs/andro-cd
cd andro-cd
cp .env.example .env      # fill in secrets
docker compose up --build
# open http://localhost:8080 and connect a manifest repository
```

A minimal manifest:

```yaml
apiVersion: andro-cd/v1
kind: ECSService
metadata:
  name: web-app
spec:
  region: eu-central-1
  cluster: production
  service: {desiredCount: 2, launchType: FARGATE}
  network:
    subnets: [subnet-aaa, subnet-bbb]
    securityGroups: [sg-0123]
  taskDefinition:
    containers:
      - name: web
        image: nginx:1.27
        portMappings: [80]
        logGroup: /ecs/web-app
```

See the [documentation](https://andro-cd.com/) for the full
manifest reference, security setup, HA, and the HTTP API.

## Architecture

```
┌─────────────┐  poll / webhook  ┌───────────────────────────┐   diff + apply   ┌─────────┐
│  Git repos  │ ───────────────► │  Andro-CD (one container) │ ───────────────► │ AWS ECS │
│  manifests  │                  │  FastAPI + React + boto3  │                  │         │
└─────────────┘                  └────────────┬──────────────┘                  └─────────┘
                                              │
                                     Postgres (history, audit,
                                     repos, profiles, HA lock)
```

- **Backend:** Python 3.12, FastAPI, boto3, SQLAlchemy
- **Frontend:** React + Vite + TypeScript
- **Deploy:** single Docker image (`ghcr.io/cyberlabrs/andro-cd`), optional Postgres

## Development

```bash
# backend
python -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests

# frontend
cd frontend && npm ci && npm run build

# docs (mkdocs-material)
pip install mkdocs-material && mkdocs serve
```

Releases are automated with [release-please](https://github.com/googleapis/release-please) —
use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, …).
See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)

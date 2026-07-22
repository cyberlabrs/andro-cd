# Contributing to Andro-CD

## Development setup

```bash
# backend (Python 3.12+)
python -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests        # 75+ tests, no AWS account needed

# frontend (Node 20+)
cd frontend && npm ci
npm run build                                    # tsc + vite

# full stack
cp .env.example .env
docker compose up --build

# docs site
pip install mkdocs-material
mkdocs serve                                     # http://localhost:8000
```

## Commit messages — Conventional Commits

Releases and the changelog are fully automated by
[release-please](https://github.com/googleapis/release-please), driven by commit
messages on `main`:

| Prefix | Effect |
|---|---|
| `fix: …` | patch release (0.1.0 → 0.1.1) |
| `feat: …` | minor release (0.1.0 → 0.2.0) |
| `feat!: …` or `BREAKING CHANGE:` footer | major release |
| `chore:`, `docs:`, `test:`, `refactor:`, `ci:` | no release, still in history |

Examples:

```
feat: add ECSTask kind for one-off jobs
fix: don't orphan apps when a repo is temporarily unreachable
docs: expand the sync-windows section
feat!: rename spec.service.launchType to spec.service.launch
```

release-please keeps a **release PR** open that accumulates changes; merging it tags
`vX.Y.Z`, creates the GitHub Release, updates `CHANGELOG.md`/`version.txt`, and the
Release workflow publishes `ghcr.io/cyberlabrs/andro-cd:vX.Y.Z` (+ `latest`).

## Pull requests

- Keep PRs focused; include tests for behavior changes (`backend/tests/`).
- `python backend/cli.py validate ./examples` must stay green.
- Update the docs when you change behavior: `docs/` (website), `backend/docs/index.md`
  (in-app guide), `SPEC.md` and `IMPROVEMENTS.md` (mark items *done*).
- CI must pass: backend tests, frontend build, docker build.

## Project layout

```
backend/app/       FastAPI app: config, models, git_sync, reconciler, engine, api, auth
backend/tests/     pytest suite (offline — no AWS calls)
frontend/          React + Vite + TypeScript UI
docs/              MkDocs Material website (GitHub Pages)
examples/          sample manifests (validated in CI)
```

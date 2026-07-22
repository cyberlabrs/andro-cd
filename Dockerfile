# --- stage 1: build the React UI ---
FROM node:20-alpine AS ui
WORKDIR /ui
# Copy package files first so node_modules layer is cached on unchanged deps (bug #36).
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- stage 2: python runtime ---
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
# Requirements first for layer cache — reinstall only when they change.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy SPEC + IMPROVEMENTS as public docs so the /docs page ships them
# without having to keep manual copies inside backend/docs/ (bug #33).
COPY SPEC.md IMPROVEMENTS.md version.txt ./
COPY backend/app ./app
COPY backend/docs ./docs
COPY backend/cli.py ./cli.py
COPY --from=ui /ui/dist ./static

# Run as a dedicated non-root user. /data holds cloned repos and the sqlite
# fallback; docs stays writable for the startup SPEC/IMPROVEMENTS refresh.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin androcd \
    && mkdir -p /data/repos /data/db \
    && chown -R androcd:androcd /data /srv/docs

ENV STATIC_DIR=/srv/static \
    REPOS_DIR=/data/repos \
    DATABASE_URL=sqlite:////data/db/andro-cd.db \
    HOME=/home/androcd \
    PORT=8080 \
    PYTHONUNBUFFERED=1
EXPOSE 8080
USER androcd

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8080')}/healthz\", timeout=4)"]

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]

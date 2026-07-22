import base64
import glob
import logging
import os
import shutil
import subprocess

import yaml

from . import templating
from .config import settings

log = logging.getLogger("andro-cd.git")


class GitError(Exception):
    pass


def repo_dir(repo: dict) -> str:
    return os.path.join(settings.repos_base_dir, f"repo-{repo['id']}")


def _ssh_key_path(repo: dict) -> str:
    return os.path.join(settings.repos_base_dir, f"repo-{repo['id']}.key")


def _resolve_auth(repo: dict) -> tuple[str, dict, str]:
    """Returns (url, extra_env, secret_to_mask) for the repo's auth type.

    Both https tokens and github_app installation tokens are passed via an
    in-memory http header (never baked into the remote URL or .git/config),
    so credentials don't leak to disk.
    """
    url = repo["url"]
    auth_type = repo.get("auth_type") or "https"

    if auth_type == "ssh" and repo.get("ssh_key"):
        key_file = _ssh_key_path(repo)
        os.makedirs(settings.repos_base_dir, exist_ok=True)
        with open(key_file, "w") as f:
            f.write(repo["ssh_key"].strip() + "\n")
        os.chmod(key_file, 0o600)
        env = {"GIT_SSH_COMMAND": (
            f"ssh -i {key_file} -o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=accept-new"
        )}
        return url, env, ""

    if auth_type == "github_app" and repo.get("github_app_id"):
        from .github_app import installation_token
        token = installation_token(
            repo["github_app_id"], repo["github_installation_id"], repo["github_private_key"]
        )
        # Pass the token via an in-memory header — never written to .git/config.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env = {"GIT_AUTH_HEADER": f"Authorization: Basic {basic}"}
        return url, env, token

    token = repo.get("token", "")
    if token and url.startswith("https://") and "@" not in url:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return url, {"GIT_AUTH_HEADER": f"Authorization: Basic {basic}"}, token
    return url, {}, token


def _git_args(env: dict) -> list[str]:
    """Extra git CLI args to inject a one-shot Authorization header (github_app)."""
    hdr = env.get("GIT_AUTH_HEADER")
    return ["-c", f"http.extraHeader={hdr}"] if hdr else []


def _run(args: list[str], cwd: str | None = None, token: str = "",
         env: dict | None = None) -> str:
    full_env = {**os.environ, **(env or {})}
    # First arg is "git"; splice `-c` overrides right after it.
    if args and args[0] == "git" and env:
        args = [args[0]] + _git_args(env) + args[1:]
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=120, env=full_env)
    if proc.returncode != 0:
        # never leak the token into logs/errors
        err = (proc.stderr or proc.stdout).replace(token or "\0", "***")
        raise GitError(f"git {' '.join(args[1:3])} failed: {err.strip()[:500]}")
    return proc.stdout.strip()


def sync_repo(repo: dict) -> dict:
    """Clone or update one repo; returns info about HEAD.

    Optimization: `git ls-remote` is a lightweight round-trip that returns just the
    branch tip's sha (~200 bytes). We short-circuit fetch when the sha hasn't moved
    since the last successful sync, avoiding a full `fetch --depth 1` transfer.
    """
    directory = repo_dir(repo)
    branch = repo.get("branch") or "main"
    auth_url, env, secret = _resolve_auth(repo)
    have_local = os.path.isdir(os.path.join(directory, ".git"))

    if have_local:
        try:
            # Cheap probe: does the remote HEAD match what we already have?
            ls = _run(["git", "ls-remote", auth_url, f"refs/heads/{branch}"],
                      token=secret, env=env)
            remote_sha = ls.split()[0] if ls else ""
            if remote_sha and remote_sha == (repo.get("commit") or ""):
                # Nothing changed — return cached HEAD info without touching disk.
                return {
                    "commit": remote_sha,
                    "message": repo.get("message") or "",
                    "author": repo.get("author") or "",
                }
            # Sha differs (or first sync since restart): do the real fetch.
            _run(["git", "remote", "set-url", "origin", auth_url],
                 cwd=directory, token=secret, env=env)
            _run(["git", "fetch", "--depth", "1", "origin", branch],
                 cwd=directory, token=secret, env=env)
            _run(["git", "reset", "--hard", f"origin/{branch}"],
                 cwd=directory, token=secret, env=env)
        except GitError:
            log.warning("fetch failed for %s, re-cloning", repo["url"])
            shutil.rmtree(directory, ignore_errors=True)
            _clone(repo, directory, auth_url, env, secret)
    else:
        _clone(repo, directory, auth_url, env, secret)

    sha = _run(["git", "rev-parse", "HEAD"], cwd=directory)
    msg = _run(["git", "log", "-1", "--pretty=%s"], cwd=directory)
    author = _run(["git", "log", "-1", "--pretty=%an"], cwd=directory)
    return {"commit": sha, "message": msg, "author": author}


def _clone(repo: dict, directory: str, auth_url: str, env: dict, secret: str) -> None:
    os.makedirs(os.path.dirname(directory), exist_ok=True)
    _run([
        "git", "clone", "--depth", "1",
        "--branch", repo.get("branch") or "main",
        auth_url, directory,
    ], token=secret, env=env)


def remove_repo_dir(repo: dict) -> None:
    shutil.rmtree(repo_dir(repo), ignore_errors=True)
    # Also delete the SSH key file (bug #7): otherwise the key stays on disk
    # after the repo is removed.
    key_file = _ssh_key_path(repo)
    if os.path.isfile(key_file):
        try:
            os.remove(key_file)
        except OSError:
            log.warning("failed to remove ssh key file for repo %s", repo["id"])


def load_manifest_docs(repo: dict) -> list[tuple[str, dict]]:
    """Returns (relative_file_path, raw_yaml_doc) for every document found in one repo.

    `values.yaml`/`values.yml` files are not manifests: they provide `${key}`
    substitutions for manifests in their directory subtree (closest file wins).
    """
    directory = repo_dir(repo)
    path = (repo.get("path") or "").strip("/")
    base = os.path.join(directory, path) if path else directory
    files = sorted(
        glob.glob(os.path.join(base, "**", "*.yaml"), recursive=True)
        + glob.glob(os.path.join(base, "**", "*.yml"), recursive=True)
    )

    # Pass 1: collect values files by directory (keys relative to the repo root).
    values_by_dir: dict[str, dict] = {}
    manifest_files: list[str] = []
    for file_path in files:
        rel = os.path.relpath(file_path, directory).replace(os.sep, "/")
        if not templating.is_values_file(rel):
            manifest_files.append(file_path)
            continue
        try:
            with open(file_path) as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                dir_key = os.path.dirname(rel).replace(os.sep, "/")
                values_by_dir[dir_key] = templating.flatten(data)
        except yaml.YAMLError as e:
            log.error("invalid values file %s: %s", rel, e)

    # Pass 2: parse manifests, applying layered ${key} substitution.
    docs: list[tuple[str, dict]] = []
    for file_path in manifest_files:
        rel = os.path.relpath(file_path, directory).replace(os.sep, "/")
        values = templating.values_for(rel, values_by_dir) if values_by_dir else {}
        try:
            with open(file_path) as f:
                for doc in yaml.safe_load_all(f):
                    if isinstance(doc, dict) and doc:
                        docs.append((rel, templating.substitute(doc, values) if values else doc))
        except yaml.YAMLError as e:
            log.error("invalid YAML in %s: %s", rel, e)
            docs.append((rel, {"__parse_error__": str(e)}))
    return docs

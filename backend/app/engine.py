import asyncio
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pydantic import ValidationError

from . import db, git_sync, metrics, notifier, reconciler
from .config import settings
from .models import Manifest, SyncPolicy
from .state import AppState, now, store
from .templating import substitute as _substitute

log = logging.getLogger("andro-cd.engine")

_restored: dict[str, dict] = {}
_mem_repo_id = 0

# Manifest parse cache — pydantic validation is the CPU hot spot at scale, so we
# key on the raw doc hash and reuse the parsed Manifest across ticks.
_manifest_cache: dict[str, tuple[str, Manifest]] = {}   # app_name -> (hash, manifest)


def _doc_hash(doc: dict) -> str:
    return hashlib.sha1(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _parse_manifest(name: str, doc: dict) -> Manifest:
    """Validate a manifest, caching by content hash so unchanged docs skip pydantic."""
    doc_hash = _doc_hash(doc)
    cached = _manifest_cache.get(name)
    if cached and cached[0] == doc_hash:
        return cached[1]
    m = Manifest.model_validate(doc)
    _manifest_cache[name] = (doc_hash, m)
    return m


def restore_state() -> None:
    """Load persisted per-app state (applied when apps are first seen)."""
    global _restored
    _restored = db.load_app_states()
    if _restored:
        log.info("restored persisted state for %d apps", len(_restored))


def load_repos() -> None:
    """Load repos from the DB, then bootstrap one from env vars if configured."""
    with store.lock():
        for r in db.list_repos():
            store.repos[r["id"]] = r
        bootstrap_missing = settings.git_repo_url and not any(
            r["url"] == settings.git_repo_url
            and (r.get("branch") or "main") == settings.git_branch
            and (r.get("path") or "") == settings.git_path
            for r in store.repos.values()
        )
    if bootstrap_missing:
        add_repo({"url": settings.git_repo_url, "branch": settings.git_branch,
                  "path": settings.git_path, "token": settings.git_token})
    log.info("tracking %d repo(s)", len(store.repos))


def load_profiles() -> None:
    with store.lock():
        for p in db.list_profiles():
            store.profiles[p["name"]] = p
        count, names = len(store.profiles), ", ".join(store.profiles)
    if count:
        log.info("loaded %d AWS profile(s): %s", count, names)


def add_profile(name: str, region: str, access_key_id: str, secret_access_key: str) -> dict:
    """Validates credentials via STS, persists encrypted, loads into memory."""
    import boto3
    from botocore.config import Config

    sts = boto3.client(
        "sts",
        region_name=region or settings.aws_region or "us-east-1",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
    )
    account_id = sts.get_caller_identity()["Account"]

    profile_id = db.add_profile(name, region, account_id, access_key_id, secret_access_key)
    profile = {"id": profile_id, "name": name, "region": region, "account_id": account_id,
               "access_key_id": access_key_id, "secret_access_key": secret_access_key}
    with store.lock():
        store.profiles[name] = profile
    reconciler.reset_client_cache()
    log.info("added AWS profile '%s' (account %s)", name, account_id)
    return store.profile_public(profile)


def remove_profile(name: str) -> bool:
    with store.lock():
        profile = store.profiles.pop(name, None)
    if not profile:
        return False
    db.delete_profile(name)
    reconciler.reset_client_cache()
    log.info("removed AWS profile '%s'", name)
    return True


def add_repo(repo_in: dict) -> dict:
    global _mem_repo_id
    repo = {
        "url": repo_in["url"],
        "branch": repo_in.get("branch") or "main",
        "path": repo_in.get("path") or "",
        "token": repo_in.get("token") or "",
        "auth_type": repo_in.get("auth_type") or "https",
        "ssh_key": repo_in.get("ssh_key") or "",
        "github_app_id": repo_in.get("github_app_id") or "",
        "github_installation_id": repo_in.get("github_installation_id") or "",
        "github_private_key": repo_in.get("github_private_key") or "",
    }
    repo_id = db.add_repo(repo)
    if repo_id is None:
        _mem_repo_id -= 1
        repo_id = _mem_repo_id
    repo["id"] = repo_id
    with store.lock():
        store.repos[repo_id] = repo
    log.info("added repo %s (auth=%s branch=%s path=%s)",
             repo["url"], repo["auth_type"], repo["branch"], repo["path"] or "/")
    return store.repo_public(repo)


def remove_repo(repo_id: int) -> bool:
    with store.lock():
        repo = store.repos.pop(repo_id, None)
        # Mark apps sourced from this repo Orphaned so they don't try to sync
        # with a stale repo reference (was a latent race before).
        if repo:
            for app in store.apps.values():
                if app.repo == repo["url"]:
                    app.sync_status = "Orphaned"
                    app.message = "repo was disconnected"
    if not repo:
        return False
    if repo_id > 0:
        db.delete_repo(repo_id)
    git_sync.remove_repo_dir(repo)
    log.info("removed repo %s", repo["url"])
    return True


def _expand_docs(docs: list[tuple[dict, str, dict]]) -> list[tuple[dict, str, dict]]:
    """Expand ECSServiceSet docs (app-of-apps) into plain ECSService docs.
    Duplicate resulting names (bug #9) become explicit errors."""
    import copy
    out: list[tuple[dict, str, dict]] = []
    seen: dict[str, str] = {}  # name -> "repo:file" for conflict messages
    for repo, rel_path, doc in docs:
        if "__parse_error__" in doc:
            out.append((repo, rel_path, doc))
            continue
        if doc.get("kind") == "ECSServiceSet":
            spec = doc.get("spec") or {}
            template = spec.get("template")
            generators = spec.get("generators") or []
            if not template or not generators:
                out.append((repo, rel_path, {"__parse_error__":
                            "ECSServiceSet requires spec.template and spec.generators"}))
                continue
            for gen in generators:
                values = (gen or {}).get("values") or {}
                rendered = _substitute(copy.deepcopy(template), values)
                out.append((repo, rel_path, rendered))
        else:
            out.append((repo, rel_path, doc))

    checked: list[tuple[dict, str, dict]] = []
    for repo, rel_path, doc in out:
        name = (doc.get("metadata") or {}).get("name")
        if not name or "__parse_error__" in doc:
            checked.append((repo, rel_path, doc))
            continue
        origin = f"{repo.get('url', '?')}:{rel_path}"
        if name in seen and seen[name] != origin:
            checked.append((repo, rel_path, {
                "__parse_error__": f"duplicate app name '{name}' also defined in {seen[name]}",
                "metadata": {"name": f"{name}@{rel_path}"},
            }))
        else:
            seen[name] = origin
            checked.append((repo, rel_path, doc))
    return checked


def _sync_coords(app: AppState) -> None:
    """Cache prune coordinates from the manifest so prune works after the manifest is gone."""
    m = app.manifest
    if not m:
        return
    app.kind = m.kind
    changed = app.prune_flag != m.spec.syncPolicy.prune
    app.prune_flag = m.spec.syncPolicy.prune
    try:
        region = reconciler._region(m)
    except Exception:
        region = m.spec.region or ""
    app.coords = {"cluster": m.spec.cluster, "region": region,
                  "aws_profile": m.spec.awsProfile or ""}
    if changed:
        _save(app)


def _save(app: AppState) -> None:
    db.save_app_state(
        app.name, app.last_synced, app.last_actions, app.last_commit, app.sync_paused,
        kind=app.kind, cluster=app.coords.get("cluster", ""),
        region=app.coords.get("region", ""), aws_profile=app.coords.get("aws_profile", ""),
        prune=app.prune_flag,
    )


def _load_apps(docs: list[tuple[dict, str, dict]], failed_urls: set[str]) -> None:
    """Parse manifest docs (from all repos) into the store."""
    seen: set[str] = set()
    docs = _expand_docs(docs)
    for repo, rel_path, doc in docs:
        if "__parse_error__" in doc:
            name = f"invalid:{rel_path}"
            app = store.apps.get(name) or AppState(name=name, file=rel_path)
            app.repo = repo["url"]
            app.sync_status, app.message = "Error", f"YAML parse error: {doc['__parse_error__'][:300]}"
            store.apps[name] = app
            seen.add(name)
            continue
        name_hint = (doc.get("metadata") or {}).get("name") or f"invalid:{rel_path}"
        try:
            manifest = _parse_manifest(name_hint, doc)
        except ValidationError as e:
            name = doc.get("metadata", {}).get("name") or f"invalid:{rel_path}"
            app = store.apps.get(name) or AppState(name=name, file=rel_path)
            app.raw, app.sync_status, app.repo = doc, "Error", repo["url"]
            app.message = f"invalid manifest: {e.errors()[0]['loc']} {e.errors()[0]['msg']}"
            store.apps[name] = app
            seen.add(name)
            continue

        app = store.apps.get(manifest.name)
        if app is None:
            app = AppState(name=manifest.name, file=rel_path)
            if manifest.name in _restored:
                restored = _restored[manifest.name]
                app.last_synced = restored["last_synced"]
                app.last_actions = restored["last_actions"]
                app.last_commit = restored.get("last_commit")
                app.sync_paused = restored.get("sync_paused", False)
        app.manifest, app.raw, app.file, app.repo = manifest, doc, rel_path, repo["url"]
        _sync_coords(app)
        if app.sync_status == "Orphaned":
            app.sync_status = "Unknown"
        store.apps[manifest.name] = app
        seen.add(manifest.name)

    # resurrect orphans persisted before a restart (manifest gone from git entirely)
    for name, restored in _restored.items():
        if name not in store.apps and restored.get("cluster"):
            orphan = AppState(name=name, file="(removed from git)")
            orphan.kind = restored.get("kind", "ECSService")
            orphan.prune_flag = restored.get("prune", False)
            orphan.coords = {"cluster": restored.get("cluster", ""),
                             "region": restored.get("region", ""),
                             "aws_profile": restored.get("aws_profile", "")}
            orphan.last_synced = restored.get("last_synced")
            orphan.sync_status = "Orphaned"
            orphan.message = "removed from Git (not deleted from AWS)"
            store.apps[name] = orphan

    for name, app in store.apps.items():
        if name not in seen:
            if app.repo and app.repo in failed_urls:
                continue  # repo temporarily unreachable — don't orphan its apps
            app.sync_status = "Orphaned"
            app.message = "removed from Git (not deleted from AWS)"


def _refresh_app(app: AppState) -> None:
    """Read-only diff for an app. Called WITHOUT the store lock (AWS I/O)."""
    if not app.manifest:
        return
    prev_health = app.health
    try:
        diff = reconciler.compute_diff(app.manifest)
    except Exception as e:
        with store.lock():
            app.sync_status, app.health = "Error", "Unknown"
            app.message = str(e)[:500]
        log.exception("diff failed for %s", app.name)
        return
    with store.lock():
        app.changes = diff["changes"]
        app.live = diff["live"]
        app.sync_status = "Synced" if diff["in_sync"] else "OutOfSync"
        app.health, app.message = reconciler.compute_health(diff["live"])
        should_notify_degraded = (
            app.health == "Degraded" and prev_health in ("Healthy", "Progressing")
        )
    if should_notify_degraded:
        notifier.notify(f":warning: *{app.name}* is degraded: {app.message}")


def _sync_app(app: AppState) -> None:
    """Runs one apply cycle for an app. Called WITHOUT the store lock."""
    if not app.manifest:
        return
    with store.lock():
        app.sync_status = "Syncing"
        commit = next(
            (r.get("commit") for r in store.repos.values() if r["url"] == app.repo), None
        )
    if settings.dry_run:
        # DRY_RUN: record the plan, never touch AWS. last_commit advances so each
        # git commit produces exactly one dry-run entry (no per-loop spam).
        with store.lock():
            planned = [f"[dry-run] {c}" for c in app.changes] or ["[dry-run] nothing to do"]
            app.last_actions = planned
            app.last_synced = now()
            app.last_commit = commit
            app.sync_status = "OutOfSync" if app.changes else "Synced"
        log.info("dry-run for %s: %s", app.name, "; ".join(planned))
        db.record_sync(app.name, commit, "DryRun", planned, "dry-run: no changes applied")
        _save(app)
        return
    started = time.monotonic()
    try:
        actions = reconciler.apply(app.manifest)
    except Exception as e:
        with store.lock():
            app.sync_status, app.message = "Error", str(e)[:500]
        log.exception("sync failed for %s", app.name)
        db.record_sync(app.name, commit, "Error", [], str(e)[:500])
        metrics.SYNC_TOTAL.labels(app=app.name, result="error").inc()
        metrics.SYNC_DURATION.labels(app=app.name).observe(time.monotonic() - started)
        notifier.notify(f":x: *{app.name}* sync failed: {str(e)[:500]}")
        _record_failure(app.name)
        return
    with store.lock():
        app.last_actions = actions or ["nothing to do"]
        app.last_synced = now()
        app.last_commit = commit
    log.info("synced %s: %s", app.name, "; ".join(actions or ["nothing to do"]))
    _clear_backoff(app.name)
    metrics.SYNC_TOTAL.labels(app=app.name, result="success").inc()
    metrics.SYNC_DURATION.labels(app=app.name).observe(time.monotonic() - started)
    if actions:
        commit_tag = f" @ `{commit[:8]}`" if commit else ""
        notifier.notify(f":rocket: *{app.name}* synced{commit_tag}: " + "; ".join(actions))
    _refresh_app(app)
    db.record_sync(app.name, commit, "Succeeded",
                   app.last_actions, app.message)
    _save(app)


_reconcile_lock = threading.Lock()


def reconcile_once(sync: bool | None = None) -> None:
    """One full loop: git pull -> parse -> diff -> (auto)apply. Blocking, serialized."""
    with _reconcile_lock:
        _reconcile(sync)


def _reconcile(sync: bool | None) -> None:
    do_sync = settings.auto_sync if sync is None else sync
    # sync=False is a read-only pass (standby replica): nothing may apply,
    # not even apps with an explicit `syncPolicy.autoSync: true` override.
    apply_allowed = sync is not False
    pass_start = time.monotonic()

    # 1. Snapshot repos (short lock), fetch git without lock (AWS/git calls are slow).
    with store.lock():
        repos = list(store.repos.values())

    all_docs: list[tuple[dict, str, dict]] = []
    failed_urls: set[str] = set()
    unchanged_repos: set[str] = set()   # repos whose HEAD sha didn't move
    for repo in repos:
        previous_sha = repo.get("commit")
        try:
            head = git_sync.sync_repo(repo)
            docs = git_sync.load_manifest_docs(repo)
        except Exception as e:
            with store.lock():
                repo["error"] = str(e)[:500]
                repo["lastPoll"] = now()
            failed_urls.add(repo["url"])
            metrics.GIT_POLL_ERRORS.inc()
            log.error("git sync failed for %s: %s", repo["url"], e)
            continue
        if head["commit"] == previous_sha:
            unchanged_repos.add(repo["url"])
            metrics.GIT_UNCHANGED_TOTAL.inc()
        with store.lock():
            repo.update(head)
            repo["error"] = None
            repo["lastPoll"] = now()
        all_docs.extend((repo, rel, doc) for rel, doc in docs)

    store.last_poll = now()
    metrics.LAST_POLL_TS.set(time.time())

    # 2. Parse & apply manifests to the store under lock (fast, no I/O).
    with store.lock():
        _load_apps(all_docs, failed_urls)
        # Snapshot immutable references — subsequent AWS calls run WITHOUT the lock.
        all_apps = list(store.apps.values())
        to_prune = [
            a for a in all_apps
            if a.sync_status == "Orphaned"
            and (a.prune_flag or (a.manifest and a.manifest.spec.syncPolicy.prune))
        ]
        active = [a for a in all_apps if a.sync_status != "Orphaned" and a.manifest]
        repo_commits = {r["url"]: r.get("commit") for r in store.repos.values()}

    # 3. Refresh diff for each active app WITHOUT the store lock.
    #
    # Optimizations:
    #  (a) `reconciler.prefetch_live_state` runs one batched DescribeClusters +
    #      DescribeServices per (region, profile) — replaces N single-service calls.
    #  (b) Diffs then run in parallel through a bounded thread pool, so 100 apps
    #      no longer wait ~100s serially.
    manifests = [a.manifest for a in active if a.manifest]
    try:
        ctx = reconciler.prefetch_live_state(manifests)
    except Exception as e:
        log.warning("prefetch failed, falling back to per-app describes: %s", e)
        ctx = None

    workers = max(1, min(settings.reconcile_workers, len(active) or 1))
    if workers > 1 and active:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            def _do(app: AppState) -> None:
                reconciler.use_prefetch(ctx)
                try:
                    _refresh_app(app)
                finally:
                    reconciler.use_prefetch(None)
            list(pool.map(_do, active))
    else:
        reconciler.use_prefetch(ctx)
        try:
            for app in active:
                _refresh_app(app)
        finally:
            reconciler.use_prefetch(None)

    # 4. Determine which apps to sync, ordered by wave.
    def _wants_sync(app: AppState) -> bool:
        if app.sync_status != "OutOfSync" or app.sync_paused:
            return False
        policy = app.manifest.spec.syncPolicy
        auto = policy.autoSync if policy.autoSync is not None else do_sync
        if not auto:
            return False
        if not in_sync_window(policy):
            return False
        commit = repo_commits.get(app.repo)
        if _in_backoff(app):
            return False
        # without selfHeal only sync when git moved (manual drift is surfaced, not reverted)
        return policy.selfHeal or app.last_commit is None or commit != app.last_commit

    def _settled(app: AppState) -> bool:
        return app.sync_status == "Synced" and app.health in ("Healthy", "Unknown")

    if apply_allowed:
        for wave in sorted({a.manifest.spec.wave for a in active}):
            lower = [a for a in active if a.manifest.spec.wave < wave]
            if lower and not all(_settled(a) for a in lower):
                log.info("wave %d deferred: lower waves not settled yet", wave)
                break
            for app in (a for a in active if a.manifest.spec.wave == wave):
                if _wants_sync(app):
                    _sync_app(app)

        # Auto-prune only when applying — standby replicas never delete anything.
        for app in to_prune:
            _prune_app(app)

    with store.lock():
        metrics.update_app_gauges(store.apps.values())
    metrics.RECONCILE_DURATION.observe(time.monotonic() - pass_start)


# ---------- sync windows ----------

def in_sync_window(policy: SyncPolicy, at: "float | None" = None) -> bool:
    """True when auto-sync is currently allowed. Empty syncWindows = always.
    Windows are UTC; start is inclusive, end exclusive (24:00 = end of day)."""
    if not policy.syncWindows:
        return True
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(at, tz=timezone.utc) if at is not None \
        else datetime.now(timezone.utc)
    day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[dt.weekday()]
    minutes = dt.hour * 60 + dt.minute

    def _mins(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    for w in policy.syncWindows:
        if day in w.days and _mins(w.start) <= minutes < _mins(w.end):
            return True
    return False


# ---------- per-app backoff (bug #13) ----------
# Exponential backoff for repeatedly-failing apps to avoid AWS throttling.
_backoff: dict[str, tuple[int, float]] = {}   # app -> (fail_count, next_attempt_epoch)


def _in_backoff(app: AppState) -> bool:
    entry = _backoff.get(app.name)
    if not entry:
        return False
    return time.time() < entry[1]


def _record_failure(name: str) -> None:
    fails, _ = _backoff.get(name, (0, 0.0))
    fails += 1
    # 30s, 60s, 2m, 4m, 8m, capped at 15m
    delay = min(30 * (2 ** (fails - 1)), 900)
    _backoff[name] = (fails, time.time() + delay)


def _clear_backoff(name: str) -> None:
    _backoff.pop(name, None)


def _prune_app(app: AppState) -> None:
    """Delete the AWS service/schedule for an app that was removed from git.
    Called WITHOUT the store lock (AWS I/O)."""
    if settings.dry_run:
        with store.lock():
            app.message = "[dry-run] would prune (delete the ECS service)"
        log.info("dry-run: would prune %s", app.name)
        return
    if not app.manifest and not (app.coords.get("cluster") and app.coords.get("region")):
        # Surface the problem to the UI; without coords we can't reach AWS.
        with store.lock():
            app.sync_status = "Error"
            app.message = "cannot prune: no manifest and no persisted coordinates"
        log.warning("cannot prune %s: no persisted coordinates", app.name)
        return
    try:
        if app.manifest:
            actions = reconciler.prune(app.manifest)
        else:
            actions = reconciler.prune_raw(
                app.name, app.kind, app.coords["cluster"],
                app.coords["region"], app.coords.get("aws_profile", ""))
    except Exception as e:
        with store.lock():
            app.sync_status, app.message = "Error", f"prune failed: {str(e)[:400]}"
        log.exception("prune failed for %s", app.name)
        return

    log.info("pruned %s: %s", app.name, "; ".join(actions))
    db.record_sync(app.name, app.last_commit, "Succeeded",
                   actions, "pruned (removed from git)")
    db.delete_app_state(app.name)
    with store.lock():
        _restored.pop(app.name, None)
        store.apps.pop(app.name, None)
    _clear_backoff(app.name)
    notifier.notify(f":wastebasket: *{app.name}* pruned: " + "; ".join(actions))


def sync_single(name: str) -> dict:
    with store.lock():
        app = store.apps.get(name)
        if not app:
            raise KeyError(name)
        app.sync_paused = False  # manual sync resumes auto-sync after a rollback
    _clear_backoff(name)
    _sync_app(app)  # lock-free AWS I/O
    with store.lock():
        return app.detail()


def rollback_single(name: str, revision: int) -> dict:
    with store.lock():
        app = store.apps.get(name)
        if not app or not app.manifest:
            raise KeyError(name)
        manifest = app.manifest  # captured while holding the lock
    if settings.dry_run:
        with store.lock():
            app.last_actions = [f"[dry-run] would roll back to revision {revision}"]
            return app.detail()
    actions = reconciler.rollback(manifest, revision)   # AWS call, no lock
    with store.lock():
        app.sync_paused = True
        app.last_actions = actions
        app.last_synced = now()
        commit = app.last_commit
    db.record_sync(name, commit, "Succeeded", actions, f"manual rollback to revision {revision}")
    _save(app)
    notifier.notify(f":rewind: *{name}* rolled back to revision {revision} (auto-sync paused)")
    _refresh_app(app)
    with store.lock():
        return app.detail()


def prune_single(name: str) -> dict:
    with store.lock():
        app = store.apps.get(name)
        if not app:
            raise KeyError(name)
        if app.sync_status != "Orphaned":
            raise ValueError("only Orphaned apps (removed from git) can be pruned")
        detail = app.detail()
    _prune_app(app)   # AWS call, no lock
    return detail


# ---------- HA / leader election ----------
# With Postgres, a session-scoped advisory lock elects exactly one applying
# replica; standbys keep polling git and refreshing diffs (read-only) so their
# UI stays live, and take over automatically when the leader dies.
_leader = True


def is_leader() -> bool:
    return _leader


async def run_loop() -> None:
    global _leader
    while True:
        try:
            leader_now = await asyncio.to_thread(db.try_acquire_leadership)
            if leader_now != _leader:
                log.warning("leadership changed: this replica is now %s",
                            "the LEADER (applying changes)" if leader_now
                            else "a STANDBY (read-only until it acquires the lock)")
            _leader = leader_now
            metrics.LEADER.set(1 if leader_now else 0)
            # Leader: normal pass (respects AUTO_SYNC). Standby: diff-only pass.
            await asyncio.to_thread(reconcile_once, None if leader_now else False)
        except Exception:
            log.exception("reconcile loop iteration failed")
        await asyncio.sleep(settings.sync_interval)

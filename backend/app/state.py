import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .models import Manifest


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AppState:
    name: str
    file: str
    repo: str = ""
    manifest: Optional[Manifest] = None
    raw: dict = field(default_factory=dict)
    sync_status: str = "Unknown"   # Synced | OutOfSync | Syncing | Error | Orphaned | Unknown
    health: str = "Unknown"        # Healthy | Progressing | Degraded | Unknown
    message: str = ""
    changes: list[str] = field(default_factory=list)
    live: dict = field(default_factory=dict)
    last_synced: Optional[str] = None
    last_actions: list[str] = field(default_factory=list)
    last_commit: Optional[str] = None   # repo commit at last successful sync
    sync_paused: bool = False           # set after a rollback; manual sync resumes
    kind: str = "ECSService"
    prune_flag: bool = False            # persisted syncPolicy.prune (survives manifest removal)
    coords: dict = field(default_factory=dict)  # cluster/region/aws_profile for manifest-less prune

    def summary(self) -> dict:
        svc = (self.live or {}).get("service") or {}
        return {
            "name": self.name,
            "file": self.file,
            "repo": self.repo,
            "kind": self.kind,
            "cluster": self.manifest.spec.cluster if self.manifest else (self.coords.get("cluster") or None),
            "region": (self.manifest.spec.region if self.manifest else None),
            "awsProfile": (self.manifest.spec.awsProfile if self.manifest else None),
            "syncStatus": self.sync_status,
            "health": self.health,
            "message": self.message,
            "changes": self.changes,
            "lastSynced": self.last_synced,
            "runningCount": svc.get("runningCount"),
            "desiredCount": svc.get("desiredCount"),
            "images": ((self.live or {}).get("taskDefinition") or {}).get("images", []),
            "labels": self.manifest.metadata.labels if self.manifest else {},
            "syncPaused": self.sync_paused,
        }

    def detail(self) -> dict:
        return {
            **self.summary(),
            "manifest": self.raw,
            "live": self.live,
            "lastActions": self.last_actions,
            "syncPolicy": (
                self.manifest.spec.syncPolicy.model_dump() if self.manifest else None
            ),
        }


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.apps: dict[str, AppState] = {}
        # repo id -> {id, url, branch, path, token, commit, message, author, error, lastPoll}
        self.repos: dict[int, dict] = {}
        # profile name -> {id, name, region, account_id, access_key_id, secret_access_key}
        self.profiles: dict[str, dict] = {}
        self.last_poll: Optional[str] = None

    def lock(self):
        return self._lock

    def repo_public(self, repo: dict) -> dict:
        return {
            "id": repo["id"],
            "url": repo["url"],
            "branch": repo.get("branch") or "main",
            "path": repo.get("path") or "",
            "authType": repo.get("auth_type") or "https",
            "hasToken": bool(repo.get("token")) or bool(repo.get("ssh_key")) or bool(repo.get("github_app_id")),
            "commit": repo.get("commit"),
            "message": repo.get("message"),
            "author": repo.get("author"),
            "error": repo.get("error"),
            "lastPoll": repo.get("lastPoll"),
        }

    def profile_public(self, p: dict) -> dict:
        key = p.get("access_key_id", "")
        return {
            "id": p.get("id"),
            "name": p["name"],
            "region": p.get("region") or "",
            "accountId": p.get("account_id") or "",
            "accessKeyId": (key[:4] + "…" + key[-4:]) if len(key) > 8 else "…",
        }

    def status(self) -> dict:
        return {
            "repos": [self.repo_public(r) for r in self.repos.values()],
            "lastPoll": self.last_poll,
            "appCount": len(self.apps),
        }


store = Store()

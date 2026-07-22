import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings

log = logging.getLogger("andro-cd.db")


class Base(DeclarativeBase):
    pass


class SyncRecord(Base):
    __tablename__ = "sync_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_name: Mapped[str] = mapped_column(String(255), index=True)
    commit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    actions: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "app": self.app_name,
            "commit": self.commit,
            "status": self.status,
            "message": self.message,
            "actions": json.loads(self.actions),
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }


class AuditRecord(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    source_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "user": self.user,
            "role": self.role,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "sourceIp": self.source_ip,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }


class AppRecord(Base):
    __tablename__ = "app_state"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_synced: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_actions: Mapped[str] = mapped_column(Text, default="[]")
    last_commit: Mapped[str] = mapped_column(String(64), default="")
    sync_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String(32), default="ECSService")
    cluster: Mapped[str] = mapped_column(String(255), default="")
    region: Mapped[str] = mapped_column(String(64), default="")
    # Match ProfileRecord.name width (String(255)) so foreign lookups line up.
    aws_profile: Mapped[str] = mapped_column(String(255), default="", index=True)
    prune: Mapped[bool] = mapped_column(Boolean, default=False)


class RepoRecord(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(1024))
    branch: Mapped[str] = mapped_column(String(255), default="main")
    path: Mapped[str] = mapped_column(String(1024), default="")
    token: Mapped[str] = mapped_column(Text, default="")
    auth_type: Mapped[str] = mapped_column(String(32), default="https")
    ssh_key: Mapped[str] = mapped_column(Text, default="")
    github_app_id: Mapped[str] = mapped_column(String(64), default="")
    github_installation_id: Mapped[str] = mapped_column(String(64), default="")
    github_private_key: Mapped[str] = mapped_column(Text, default="")

    def as_dict(self) -> dict:
        return {"id": self.id, "url": self.url, "branch": self.branch,
                "path": self.path, "token": self.token,
                "auth_type": self.auth_type or "https",
                "ssh_key": self.ssh_key or "",
                "github_app_id": self.github_app_id or "",
                "github_installation_id": self.github_installation_id or "",
                "github_private_key": self.github_private_key or ""}


class ProfileRecord(Base):
    __tablename__ = "aws_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    region: Mapped[str] = mapped_column(String(64), default="")
    account_id: Mapped[str] = mapped_column(String(32), default="")
    access_key_enc: Mapped[str] = mapped_column(Text, default="")
    secret_key_enc: Mapped[str] = mapped_column(Text, default="")


_Session: Optional[sessionmaker] = None
_engine = None


def init_db() -> bool:
    global _Session, _engine
    url = settings.database_url
    if not url:
        log.warning("DATABASE_URL not set — persistence disabled, state is in-memory only")
        return False
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    try:
        engine = create_engine(url, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        _migrate(engine)
        _Session = sessionmaker(engine, expire_on_commit=False)
        _engine = engine
        log.info("persistence enabled (%s)", url.split("@")[-1])
        return True
    except Exception as e:
        log.error("failed to initialize database, persistence disabled: %s", e)
        _Session = None
        return False


def _migrate(engine) -> None:
    """Add columns introduced after the table was first created (create_all won't)."""
    from sqlalchemy import inspect, text

    wanted = {
        "repos": {
            "auth_type": "VARCHAR(32) DEFAULT 'https'",
            "ssh_key": "TEXT DEFAULT ''",
            "github_app_id": "VARCHAR(64) DEFAULT ''",
            "github_installation_id": "VARCHAR(64) DEFAULT ''",
            "github_private_key": "TEXT DEFAULT ''",
        },
        "app_state": {
            "last_commit": "VARCHAR(64) DEFAULT ''",
            "sync_paused": "BOOLEAN DEFAULT FALSE",
            "kind": "VARCHAR(32) DEFAULT 'ECSService'",
            "cluster": "VARCHAR(255) DEFAULT ''",
            "region": "VARCHAR(64) DEFAULT ''",
            "aws_profile": "VARCHAR(255) DEFAULT ''",
            "prune": "BOOLEAN DEFAULT FALSE",
        },
    }
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in wanted.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    log.info("migrated %s table: added column %s", table, name)


# ---------- HA leader election (Postgres advisory lock) ----------

LEADER_LOCK_KEY = 0x65637361_7267_6F00 % (2 ** 63)   # stable app-wide key ("androcd")
_leader_conn = None


def try_acquire_leadership() -> bool:
    """Session-scoped Postgres advisory lock: exactly one replica gets it and
    keeps it for the lifetime of its dedicated connection. If the leader dies,
    Postgres releases the lock and a standby acquires it on its next attempt.
    Non-Postgres backends (sqlite / no DB) are single-instance: always leader."""
    global _leader_conn
    if _engine is None or _engine.dialect.name != "postgresql":
        return True
    from sqlalchemy import text
    try:
        if _leader_conn is None or _leader_conn.closed:
            _leader_conn = _engine.connect()
        got = _leader_conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": LEADER_LOCK_KEY}
        ).scalar()
        _leader_conn.rollback()   # end the implicit transaction; the lock is session-scoped
        return bool(got)
    except Exception as e:
        log.warning("leadership check failed (treating as standby): %s", e)
        try:
            if _leader_conn is not None:
                _leader_conn.close()
        except Exception:
            pass
        _leader_conn = None
        return False


def release_leadership() -> None:
    """Close the lock connection (releases the advisory lock server-side)."""
    global _leader_conn
    if _leader_conn is not None:
        try:
            _leader_conn.close()
        except Exception:
            pass
        _leader_conn = None


def ready() -> bool:
    """True when persistence is configured and the database answers a ping."""
    if not _Session:
        return False
    from sqlalchemy import text
    try:
        with _Session() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def record_sync(app_name: str, commit: Optional[str], status: str,
                actions: list[str], message: str) -> None:
    if not _Session:
        return
    try:
        with _Session.begin() as s:
            s.add(SyncRecord(
                app_name=app_name, commit=commit, status=status,
                message=message[:2000], actions=json.dumps(actions),
            ))
    except Exception as e:
        log.error("failed to record sync for %s: %s", app_name, e)


def save_app_state(name: str, last_synced: Optional[str], last_actions: list[str],
                   last_commit: Optional[str] = None, sync_paused: bool = False,
                   kind: str = "ECSService", cluster: str = "", region: str = "",
                   aws_profile: str = "", prune: bool = False) -> None:
    if not _Session:
        return
    try:
        with _Session.begin() as s:
            rec = s.get(AppRecord, name) or AppRecord(name=name)
            rec.last_synced = last_synced
            rec.last_actions = json.dumps(last_actions)
            rec.last_commit = last_commit or ""
            rec.sync_paused = sync_paused
            rec.kind = kind
            rec.cluster = cluster
            rec.region = region
            rec.aws_profile = aws_profile
            rec.prune = prune
            s.add(rec)
    except Exception as e:
        log.error("failed to save state for %s: %s", name, e)


def delete_app_state(name: str) -> None:
    if not _Session:
        return
    try:
        with _Session.begin() as s:
            rec = s.get(AppRecord, name)
            if rec:
                s.delete(rec)
    except Exception as e:
        log.error("failed to delete state for %s: %s", name, e)


def load_app_states() -> dict[str, dict]:
    if not _Session:
        return {}
    try:
        with _Session() as s:
            return {
                r.name: {
                    "last_synced": r.last_synced,
                    "last_actions": json.loads(r.last_actions),
                    "last_commit": r.last_commit or None,
                    "sync_paused": bool(r.sync_paused),
                    "kind": r.kind or "ECSService",
                    "cluster": r.cluster or "",
                    "region": r.region or "",
                    "aws_profile": r.aws_profile or "",
                    "prune": bool(r.prune),
                }
                for r in s.scalars(select(AppRecord))
            }
    except Exception as e:
        log.error("failed to load app states: %s", e)
        return {}


def list_repos() -> list[dict]:
    if not _Session:
        return []
    try:
        with _Session() as s:
            return [r.as_dict() for r in s.scalars(select(RepoRecord))]
    except Exception as e:
        log.error("failed to list repos: %s", e)
        return []


def add_repo(repo: dict) -> Optional[int]:
    """Persist a repo; returns its id, or None when persistence is unavailable."""
    if not _Session:
        return None
    try:
        with _Session.begin() as s:
            rec = RepoRecord(
                url=repo["url"], branch=repo.get("branch", "main"),
                path=repo.get("path", ""), token=repo.get("token", ""),
                auth_type=repo.get("auth_type", "https"),
                ssh_key=repo.get("ssh_key", ""),
                github_app_id=repo.get("github_app_id", ""),
                github_installation_id=repo.get("github_installation_id", ""),
                github_private_key=repo.get("github_private_key", ""),
            )
            s.add(rec)
            s.flush()
            return rec.id
    except Exception as e:
        log.error("failed to persist repo %s: %s", repo.get("url"), e)
        return None


def delete_repo(repo_id: int) -> None:
    if not _Session:
        return
    try:
        with _Session.begin() as s:
            rec = s.get(RepoRecord, repo_id)
            if rec:
                s.delete(rec)
    except Exception as e:
        log.error("failed to delete repo %s: %s", repo_id, e)


def list_profiles() -> list[dict]:
    """Returns profiles with decrypted credentials. Undecryptable ones are skipped."""
    if not _Session:
        return []
    from . import crypto
    out = []
    try:
        with _Session() as s:
            for r in s.scalars(select(ProfileRecord)):
                try:
                    out.append({
                        "id": r.id, "name": r.name, "region": r.region or "",
                        "account_id": r.account_id or "",
                        "access_key_id": crypto.decrypt(r.access_key_enc),
                        "secret_access_key": crypto.decrypt(r.secret_key_enc),
                    })
                except crypto.DecryptError as e:
                    log.error("skipping AWS profile '%s': %s", r.name, e)
    except Exception as e:
        log.error("failed to list AWS profiles: %s", e)
    return out


def add_profile(name: str, region: str, account_id: str,
                access_key_id: str, secret_access_key: str) -> Optional[int]:
    if not _Session:
        return None
    from . import crypto
    try:
        with _Session.begin() as s:
            rec = ProfileRecord(
                name=name, region=region, account_id=account_id,
                access_key_enc=crypto.encrypt(access_key_id),
                secret_key_enc=crypto.encrypt(secret_access_key),
            )
            s.add(rec)
            s.flush()
            return rec.id
    except Exception as e:
        log.error("failed to persist AWS profile '%s': %s", name, e)
        return None


def delete_profile(name: str) -> None:
    if not _Session:
        return
    try:
        with _Session.begin() as s:
            for rec in s.scalars(select(ProfileRecord).where(ProfileRecord.name == name)):
                s.delete(rec)
    except Exception as e:
        log.error("failed to delete AWS profile '%s': %s", name, e)


def record_audit(user: str, role: str, action: str, target: str = "",
                 detail: str = "", source_ip: str = "") -> None:
    """Persist one audit event (who did what, when, from where). Never raises."""
    if not _Session:
        return
    try:
        with _Session.begin() as s:
            s.add(AuditRecord(
                user=user[:255], role=role[:32], action=action[:64],
                target=target[:255], detail=detail[:2000], source_ip=source_ip[:64],
            ))
    except Exception as e:
        log.error("failed to record audit event %s/%s: %s", user, action, e)


def list_audit(limit: int = 100, user: Optional[str] = None,
               action: Optional[str] = None) -> list[dict]:
    if not _Session:
        return []
    try:
        with _Session() as s:
            q = select(AuditRecord).order_by(AuditRecord.id.desc()).limit(limit)
            if user:
                q = q.where(AuditRecord.user == user)
            if action:
                q = q.where(AuditRecord.action == action)
            return [r.as_dict() for r in s.scalars(q)]
    except Exception as e:
        log.error("failed to list audit log: %s", e)
        return []


def get_history(app_name: str, limit: int = 20) -> list[dict]:
    if not _Session:
        return []
    try:
        with _Session() as s:
            rows = s.scalars(
                select(SyncRecord)
                .where(SyncRecord.app_name == app_name)
                .order_by(SyncRecord.id.desc())
                .limit(limit)
            )
            return [r.as_dict() for r in rows]
    except Exception as e:
        log.error("failed to load history for %s: %s", app_name, e)
        return []

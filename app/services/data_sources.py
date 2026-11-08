"""Connected data sources: persistence, validation, registration and backups."""

from __future__ import annotations

import logging
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import Settings
from app.connectors.folder_source import FolderSourceEnvironment
from app.connectors.practice_store import generate as generate_practice_store
from app.connectors.sqlite_source import SourceConfig, SQLiteSourceEnvironment
from app.database import repositories as repo
from app.database.connection import Database
from app.database.models import DataSource, RunBackup
from app.environments.base import Environment
from app.environments.registry import EnvironmentRegistry
from app.utils.timeutil import utcnow

log = logging.getLogger("proofloop.sources")
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db3"}
PRACTICE_NAME = "Practice store"


class SourceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    location: str = Field(min_length=1, max_length=1000, description="Absolute path of a SQLite file or a folder")
    kind: str | None = Field(None, description="sqlite | folder (detected when omitted)")
    excluded: list[str] = Field(default_factory=list, max_length=200)
    exclusive: bool = True
    max_rows_changed: int = Field(200, ge=1, le=100_000)
    max_snapshot_rows: int = Field(50_000, ge=100, le=1_000_000)


def _config(rec: DataSource) -> SourceConfig:
    return SourceConfig(id=rec.id, name=rec.name, kind=rec.kind, location=rec.location, excluded=list(rec.excluded or []),
                        exclusive=rec.exclusive, max_rows_changed=rec.max_rows_changed, max_snapshot_rows=rec.max_snapshot_rows)


def build_environment(config: SourceConfig) -> Environment:
    if config.kind == "sqlite":
        return SQLiteSourceEnvironment(config)
    if config.kind == "folder":
        return FolderSourceEnvironment(config)
    raise SourceError("invalid_kind", f"Unsupported source type {config.kind!r}.")


class DataSourceService:
    def __init__(self, settings: Settings, db: Database, registry: EnvironmentRegistry) -> None:
        self.settings = settings
        self.db = db
        self.registry = registry
        self.errors: dict[str, str] = {}  # source id -> why it could not be loaded

    @property
    def backup_dir(self) -> Path:
        return self.settings.data_dir / "backups"

    @property
    def practice_path(self) -> Path:
        return self.settings.data_dir / "practice" / "store.db"

    # ---------------------------------------------------------------- startup
    def load_all(self) -> None:
        self._ensure_practice_store()
        with self.db.session() as s:
            records = list(s.scalars(select(DataSource).where(DataSource.enabled.is_(True))))
            configs = [_config(r) for r in records]
        for cfg in configs:
            self._register(cfg)

    def _register(self, cfg: SourceConfig) -> Environment | None:
        try:
            env = build_environment(cfg)
        except (OSError, sqlite3.Error, SourceError, NotImplementedError) as exc:
            self.errors[cfg.id] = str(exc)
            self.registry.unregister(f"src_{cfg.id}")
            log.warning("Data source %s (%s) unavailable: %s", cfg.name, cfg.location, exc)
            return None
        self.errors.pop(cfg.id, None)
        self.registry.register(env)
        return env

    def _ensure_practice_store(self) -> None:
        with self.db.session() as s:
            exists = s.scalar(select(DataSource).where(DataSource.is_practice.is_(True)))
            if exists is not None:
                if not Path(exists.location).is_file():
                    generate_practice_store(Path(exists.location))
                return
            if not self.practice_path.is_file():
                generate_practice_store(self.practice_path)
            s.add(DataSource(id="practice", name=PRACTICE_NAME, kind="sqlite", location=str(self.practice_path),
                             excluded=[], exclusive=True, is_practice=True, created_by="system"))

    # ------------------------------------------------------------------ CRUD
    def list(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            records = list(s.scalars(select(DataSource).order_by(DataSource.is_practice.desc(), DataSource.created_at)))
            return [self.describe(r) for r in records]

    def describe(self, rec: DataSource) -> dict[str, Any]:
        name = f"src_{rec.id}"
        env = self.registry.get(name) if name in self.registry.names() else None
        info: dict[str, Any] = {
            "id": rec.id, "environment": name, "name": rec.name, "kind": rec.kind, "location": rec.location,
            "excluded": rec.excluded or [], "exclusive": rec.exclusive, "max_rows_changed": rec.max_rows_changed,
            "max_snapshot_rows": rec.max_snapshot_rows, "is_practice": rec.is_practice, "enabled": rec.enabled,
            "created_by": rec.created_by, "created_at": rec.created_at.isoformat() + "Z",
            "status": "connected" if env else ("disabled" if not rec.enabled else "unavailable"),
            "error": self.errors.get(rec.id), "warnings": list(getattr(env, "warnings", []) or []),
            "entities": [],
        }
        if env:
            for e in env.schema.entities:
                info["entities"].append({"name": e.name, "collection": e.collection, "display_name": e.display_name,
                                         "key": e.key_field, "fields": [f.name for f in e.fields]})
        return info

    def get(self, source_id: str) -> DataSource:
        with self.db.session() as s:
            rec = s.get(DataSource, source_id)
            if rec is None:
                raise SourceError("not_found", "Data source not found.", 404)
            s.expunge(rec)
            return rec

    def _validate_location(self, location: str, kind: str | None) -> tuple[Path, str]:
        path = Path(location.strip().strip('"')).expanduser()
        if not path.is_absolute():
            raise SourceError("invalid_location", "Use the full path, e.g. C:\\Data\\shop.db or C:\\Data\\Documents.")
        try:
            path = path.resolve(strict=True)
        except OSError:
            raise SourceError("invalid_location", f"Nothing exists at {path}.") from None
        data_dir = self.settings.data_dir.resolve()
        if path == data_dir or data_dir in path.parents or path in data_dir.parents:
            raise SourceError("invalid_location", "ProofLoop's own data folder (or a folder containing it) cannot be connected.")
        detected = "folder" if path.is_dir() else "sqlite"
        kind = kind or detected
        if kind == "folder" and not path.is_dir():
            raise SourceError("invalid_location", "The path is a file, not a folder.")
        if kind == "sqlite":
            if not path.is_file():
                raise SourceError("invalid_location", "The path is a folder, not a SQLite file.")
            with open(path, "rb") as fh:
                if fh.read(16) != b"SQLite format 3\x00":
                    raise SourceError("invalid_location", f"{path.name} is not a SQLite database.")
        if kind not in ("sqlite", "folder"):
            raise SourceError("invalid_kind", "Supported source types: sqlite, folder.")
        return path, kind

    def create(self, body: SourceCreate, actor: str) -> dict[str, Any]:
        path, kind = self._validate_location(body.location, body.kind)
        with self.db.session() as s:
            if s.scalar(select(DataSource).where(DataSource.location == str(path))):
                raise SourceError("duplicate", "This location is already connected.", 409)
        cfg = SourceConfig(id=secrets.token_hex(4), name=body.name.strip(), kind=kind, location=str(path),
                           excluded=[e.strip() for e in body.excluded if e.strip()], exclusive=body.exclusive,
                           max_rows_changed=body.max_rows_changed, max_snapshot_rows=body.max_snapshot_rows)
        try:
            env = build_environment(cfg)
            env.inspector().snapshot()  # proves read-only access works before anything is saved
        except Exception as exc:  # noqa: BLE001 - report any connection problem to the user
            raise SourceError("connection_failed", f"Could not connect: {exc}") from exc
        with self.db.session() as s:
            s.add(DataSource(id=cfg.id, name=cfg.name, kind=kind, location=cfg.location, excluded=cfg.excluded,
                             exclusive=cfg.exclusive, max_rows_changed=cfg.max_rows_changed,
                             max_snapshot_rows=cfg.max_snapshot_rows, created_by=actor))
            repo.audit(s, actor, "source_connected", f"{cfg.name} ({kind}) {cfg.location}")
        self.registry.register(env)
        return self.describe(self.get(cfg.id))

    def refresh(self, source_id: str) -> dict[str, Any]:
        rec = self.get(source_id)
        self._register(_config(rec))
        return self.describe(rec)

    def remove(self, source_id: str, actor: str) -> None:
        with self.db.session() as s:
            rec = s.get(DataSource, source_id)
            if rec is None:
                raise SourceError("not_found", "Data source not found.", 404)
            if rec.is_practice:
                raise SourceError("protected", "The practice store cannot be removed (reset it instead).", 409)
            s.delete(rec)
            repo.audit(s, actor, "source_disconnected", f"{rec.name} {rec.location}")
        self.registry.unregister(f"src_{source_id}")
        self.errors.pop(source_id, None)

    def reset_practice(self, actor: str) -> dict[str, int]:
        env = self.registry.get("src_practice")
        with env.lock:
            counts = generate_practice_store(self.practice_path)
            self._register(_config(self.get("practice")))
        with self.db.session() as s:
            repo.audit(s, actor, "practice_store_reset", str(counts))
        return counts

    # ---------------------------------------------------------------- backups
    def backup(self, env: Environment, execution_id: str) -> RunBackup:
        suffix = ".zip" if isinstance(env, FolderSourceEnvironment) else ".sqlite"
        dest = self.backup_dir / f"{execution_id}{suffix}"
        size = env.backup(dest)  # type: ignore[attr-defined]
        rec = RunBackup(execution_id=execution_id, environment=env.name, path=str(dest), size_bytes=size)
        with self.db.session() as s:
            s.add(rec)
        return rec

    def latest_backup(self, environment: str) -> RunBackup | None:
        with self.db.session() as s:
            rec = s.scalar(select(RunBackup).where(RunBackup.environment == environment)
                           .order_by(RunBackup.created_at.desc()).limit(1))
            if rec is not None:
                s.expunge(rec)
            return rec

    def backup_for(self, execution_id: str) -> RunBackup | None:
        with self.db.session() as s:
            rec = s.get(RunBackup, execution_id)
            if rec is not None:
                s.expunge(rec)
            return rec

    def mark_restored(self, execution_id: str, actor: str) -> None:
        with self.db.session() as s:
            rec = s.get(RunBackup, execution_id)
            if rec is not None:
                rec.restored_at, rec.restored_by = utcnow(), actor
            repo.audit(s, actor, "run_undone", execution_id)

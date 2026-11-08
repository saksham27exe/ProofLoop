"""Shared FastAPI dependencies: application context, DB sessions, auth."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.auth.service import AuthService, role_at_least
from app.config import Settings
from app.database.connection import Database
from app.database.models import User
from app.environments.registry import EnvironmentRegistry
from app.llm.model_manager import ModelManager
from app.services.data_sources import DataSourceService
from app.services.execution_service import ExecutionService
from app.utils.timeutil import utcnow


@dataclass
class AppContext:
    settings: Settings
    db: Database
    registry: EnvironmentRegistry
    models: ModelManager
    auth: AuthService
    executions: ExecutionService
    sources: DataSourceService
    benchmark_lock: threading.Lock = field(default_factory=threading.Lock)
    started_at: datetime = field(default_factory=utcnow)


def build_context(settings: Settings) -> AppContext:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.create_all()
    registry = EnvironmentRegistry(settings.sandbox_dir, settings.sample_data_dir)
    registry.initialize()
    sources = DataSourceService(settings, db, registry)
    sources.load_all()
    models = ModelManager(settings)
    return AppContext(
        settings=settings, db=db, registry=registry, models=models, auth=AuthService(settings),
        executions=ExecutionService(settings, db, registry, models, sources=sources), sources=sources,
    )


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_session(ctx: AppContext = Depends(get_ctx)) -> Iterator[Session]:
    session = ctx.db.new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_token(token: str | None = Depends(oauth2_scheme)) -> str | None:
    return token


def current_user(
    token: str | None = Depends(oauth2_scheme),
    ctx: AppContext = Depends(get_ctx),
    session: Session = Depends(get_session),
) -> User:
    user = ctx.auth.resolve_token(session, token or "")
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "not_authenticated", "message": "Sign in required (missing, invalid or expired session)."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_role(minimum: str) -> Callable[[User], User]:
    def dependency(user: User = Depends(current_user)) -> User:
        if not role_at_least(user.role, minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": f"This action requires the '{minimum}' role."},
            )
        return user

    return dependency

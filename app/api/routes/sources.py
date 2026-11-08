"""Connected data sources (real data): list, connect, refresh, disconnect, practice-store reset."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import AppContext, current_user, get_ctx, require_role
from app.database.models import User
from app.services.data_sources import SourceCreate, SourceError

router = APIRouter(prefix="/sources", tags=["data sources"])


def _err(err: SourceError) -> HTTPException:
    return HTTPException(status_code=err.status_code, detail={"code": err.code, "message": err.message})


@router.get("")
def list_sources(_: User = Depends(current_user), ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    return ctx.sources.list()


@router.post("", status_code=201)
def connect_source(body: SourceCreate, admin: User = Depends(require_role("admin")),
                   ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Connect a SQLite database file or a folder. Read access is tested before anything is saved."""
    try:
        return ctx.sources.create(body, admin.username)
    except SourceError as err:
        raise _err(err) from None


@router.post("/practice/reset")
def reset_practice(user: User = Depends(require_role("analyst")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Regenerate the practice store (fictional data) to its original state."""
    try:
        counts = ctx.sources.reset_practice(user.username)
    except (SourceError, KeyError) as err:
        raise HTTPException(status_code=409, detail={"code": "reset_failed", "message": str(err)}) from None
    ctx.executions.bump_env_version("src_practice")
    return {"status": "reset", "rows": counts}


@router.post("/{source_id}/refresh")
def refresh_source(source_id: str, _: User = Depends(require_role("analyst")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Re-read the structure (tables/columns) of a source, e.g. after its schema changed."""
    try:
        return ctx.sources.refresh(source_id)
    except SourceError as err:
        raise _err(err) from None


@router.delete("/{source_id}")
def disconnect_source(source_id: str, admin: User = Depends(require_role("admin")),
                      ctx: AppContext = Depends(get_ctx)) -> dict[str, str]:
    """Disconnect a source. Its data is not touched; past executions and reports are kept."""
    try:
        ctx.sources.remove(source_id, admin.username)
    except SourceError as err:
        raise _err(err) from None
    return {"status": "disconnected", "id": source_id}

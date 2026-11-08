"""Benchmark endpoints."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.dependencies import AppContext, current_user, get_ctx, require_role
from app.benchmark.runner import markdown_summary, run_benchmark, save_results
from app.database import repositories as repo
from app.database.models import User

router = APIRouter(prefix="/benchmark", tags=["benchmark"])


class BenchmarkRequest(BaseModel):
    seed: int = Field(42, ge=0, le=10_000_000)
    variants: int = Field(3, ge=1, le=10)
    interpreter: Literal["rules", "llm", "auto"] = "rules"
    model: str | None = Field(None, max_length=100, pattern=r"^[A-Za-z0-9._:/\-]+$")


@router.post("/run")
def run(body: BenchmarkRequest, user: User = Depends(require_role("analyst")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Run the full benchmark synchronously in an isolated temporary sandbox."""
    if not ctx.benchmark_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail={"code": "busy", "message": "A benchmark is already running."})
    try:
        results = run_benchmark(ctx.settings, seed=body.seed, variants=body.variants, interpreter=body.interpreter,
                                model=body.model)
        save_results(results, ctx.settings.benchmark_dir)
    finally:
        ctx.benchmark_lock.release()
    with ctx.db.session() as s:
        repo.audit(s, user.username, "benchmark_run", f"{results['dataset']['size']} cases, interpreter={body.interpreter}")
    return {**results, "markdown": markdown_summary(results)}


@router.get("/latest")
def latest(_: User = Depends(current_user), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    path = ctx.settings.benchmark_dir / "latest.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "No benchmark has been run yet."})
    results = json.loads(path.read_text(encoding="utf-8"))
    return {**results, "markdown": markdown_summary(results)}

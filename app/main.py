"""FastAPI application factory.

Run with:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.dependencies import AppContext, build_context
from app.api.routes import auth, benchmark, executions, health, platform, reports, sources
from app.config import Settings, get_settings
from app.utils.logging import configure_logging, get_logger

log = get_logger("api")

DESCRIPTION = """
**ProofLoop** independently verifies whether AI agents actually accomplished the tasks they claim.

*Don't trust what an AI agent says it did. Verify what actually happened.*

Use **Authorize** (top right) with your ProofLoop username and password. All data stays on this machine.
"""


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}}, headers=headers)


def create_app(settings: Settings | None = None, context: AppContext | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.logs_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx = context or build_context(settings)
        log.info("ProofLoop %s ready - data directory: %s", __version__, settings.data_dir)
        yield
        app.state.ctx.db.dispose()

    app = FastAPI(
        title="ProofLoop API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        swagger_ui_parameters={"persistAuthorization": False, "displayRequestDuration": True},
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return _error(exc.status_code, detail["code"], detail.get("message", ""), getattr(exc, "headers", None))
        return _error(exc.status_code, "http_error", str(detail), getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for e in exc.errors()[:5]:
            loc = ".".join(str(p) for p in e.get("loc", []) if p != "body")
            problems.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
        return _error(422, "validation_error", "; ".join(problems))

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error: %s", exc)
        return _error(500, "internal_error", "An internal error occurred. Details were written to the local log file.")

    for router in (health.router, auth.router, executions.router, reports.router, platform.router, benchmark.router,
                   sources.router):
        app.include_router(router)
    return app


app = create_app()

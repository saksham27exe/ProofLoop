"""Authentication and user-management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import (
    AppContext,
    current_user,
    get_ctx,
    get_session,
    get_token,
    require_role,
)
from app.api.schemas import (
    BootstrapRequest,
    LoginRequest,
    PasswordChange,
    PasswordReset,
    TokenResponse,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.auth.service import AuthError
from app.database import repositories as repo
from app.database.models import User
from app.utils.timeutil import iso

router = APIRouter(tags=["auth"])


def _http(err: AuthError) -> HTTPException:
    return HTTPException(status_code=err.status_code, detail={"code": err.code, "message": err.message})


def _login(ctx: AppContext, session: Session, username: str, password: str) -> TokenResponse:
    try:
        result = ctx.auth.authenticate(session, username, password)
    except AuthError as err:
        session.commit()  # persist failed-attempt counters and audit entries
        raise _http(err) from None
    session.commit()
    return TokenResponse(access_token=result.token, expires_at=iso(result.expires_at) or "", user=UserOut.of(result.user))


@router.get("/auth/bootstrap-status")
def bootstrap_status(ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> dict:
    return {"needs_bootstrap": ctx.auth.needs_bootstrap(session)}


@router.post("/auth/bootstrap", response_model=TokenResponse, status_code=201)
def bootstrap(body: BootstrapRequest, ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> TokenResponse:
    """Create the first administrator. Only allowed while no user exists."""
    try:
        ctx.auth.bootstrap_admin(session, body.username, body.password, body.full_name)
    except AuthError as err:
        raise _http(err) from None
    session.commit()
    return _login(ctx, session, body.username, body.password)


@router.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> TokenResponse:
    return _login(ctx, session, body.username, body.password)


@router.post("/auth/token", response_model=TokenResponse, include_in_schema=True)
def token(form: OAuth2PasswordRequestForm = Depends(), ctx: AppContext = Depends(get_ctx),
          session: Session = Depends(get_session)) -> TokenResponse:
    """OAuth2 password flow (used by the 'Authorize' button in /docs)."""
    return _login(ctx, session, form.username, form.password)


@router.post("/auth/logout")
def logout(token: str | None = Depends(get_token), user: User = Depends(current_user), ctx: AppContext = Depends(get_ctx),
           session: Session = Depends(get_session)) -> dict:
    if token:
        ctx.auth.logout(session, token)
    session.commit()
    return {"status": "signed_out"}


@router.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.of(user)


@router.post("/auth/change-password")
def change_password(body: PasswordChange, token: str | None = Depends(get_token), user: User = Depends(current_user),
                    ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> dict:
    try:
        ctx.auth.change_password(session, user, body.current_password, body.new_password, keep_token=token)
    except AuthError as err:
        raise _http(err) from None
    session.commit()
    return {"status": "password_changed", "other_sessions_revoked": True}


# ------------------------------------------------------------------ admin
@router.get("/users", response_model=list[UserOut], tags=["users"])
def list_users(_: User = Depends(require_role("admin")), session: Session = Depends(get_session)) -> list[UserOut]:
    return [UserOut.of(u) for u in session.scalars(select(User).order_by(User.username))]


@router.post("/users", response_model=UserOut, status_code=201, tags=["users"])
def create_user(body: UserCreate, admin: User = Depends(require_role("admin")), ctx: AppContext = Depends(get_ctx),
                session: Session = Depends(get_session)) -> UserOut:
    try:
        user = ctx.auth.create_user(session, body.username, body.password, body.full_name, body.role, actor=admin.username)
    except AuthError as err:
        raise _http(err) from None
    user.must_change_password = True
    session.commit()
    return UserOut.of(user)


@router.patch("/users/{user_id}", response_model=UserOut, tags=["users"])
def update_user(user_id: int, body: UserUpdate, admin: User = Depends(require_role("admin")),
                ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> UserOut:
    try:
        user = ctx.auth.update_user(session, user_id, actor=admin, role=body.role, is_active=body.is_active,
                                    full_name=body.full_name)
    except AuthError as err:
        raise _http(err) from None
    session.commit()
    return UserOut.of(user)


@router.post("/users/{user_id}/reset-password", response_model=UserOut, tags=["users"])
def reset_password(user_id: int, body: PasswordReset, admin: User = Depends(require_role("admin")),
                   ctx: AppContext = Depends(get_ctx), session: Session = Depends(get_session)) -> UserOut:
    try:
        user = ctx.auth.admin_reset_password(session, user_id, body.new_password, actor=admin)
    except AuthError as err:
        raise _http(err) from None
    session.commit()
    return UserOut.of(user)


@router.get("/audit", tags=["users"])
def audit_log(limit: int = 100, _: User = Depends(require_role("admin")), session: Session = Depends(get_session)) -> list[dict]:
    limit = max(1, min(limit, 500))
    return [{"id": a.id, "actor": a.actor, "action": a.action, "detail": a.detail, "created_at": iso(a.created_at)}
            for a in repo.list_audit(session, limit)]

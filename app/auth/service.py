"""Local user accounts, sessions and role-based access.

* Passwords: PBKDF2-SHA256 (see :mod:`app.auth.passwords`).
* Sessions: random 256-bit bearer tokens; only their SHA-256 is stored.
* Brute-force protection: account lock after N consecutive failures.
* Roles: ``viewer`` < ``analyst`` < ``admin``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.passwords import (
    hash_password,
    needs_rehash,
    password_problems,
    valid_username,
    verify_password,
)
from app.config import Settings
from app.database import repositories as repo
from app.database.models import AuthSession, User
from app.utils.hashing import sha256_hex
from app.utils.timeutil import utcnow

ROLES = ("viewer", "analyst", "admin")
ROLE_RANK = {role: rank for rank, role in enumerate(ROLES)}


class AuthError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class LoginResult:
    user: User
    token: str
    expires_at: object


def role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[minimum]


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Pre-computed hash used to equalise timing for unknown usernames.
        self._dummy_hash = hash_password("timing-equaliser-0", settings.password_iterations)

    # ------------------------------------------------------------------ users
    def _validate_new_credentials(self, username: str, password: str) -> None:
        if not valid_username(username):
            raise AuthError(
                "invalid_username",
                "Username must be 3-32 characters: letters, digits, '.', '_' or '-' (starting with a letter or digit).",
            )
        problems = password_problems(password, username)
        if problems:
            raise AuthError("weak_password", "Password " + "; ".join(problems) + ".")

    def needs_bootstrap(self, session: Session) -> bool:
        return repo.count_users(session) == 0

    def bootstrap_admin(self, session: Session, username: str, password: str, full_name: str = "") -> User:
        if not self.needs_bootstrap(session):
            raise AuthError("already_initialized", "An administrator account already exists.", 409)
        user = self.create_user(session, username, password, full_name, "admin", actor="bootstrap")
        repo.audit(session, username, "bootstrap_admin", "First administrator account created")
        return user

    def create_user(
        self, session: Session, username: str, password: str, full_name: str, role: str, actor: str
    ) -> User:
        username = username.strip()
        self._validate_new_credentials(username, password)
        if role not in ROLES:
            raise AuthError("invalid_role", f"Role must be one of {', '.join(ROLES)}.")
        if repo.get_user_by_username(session, username):
            raise AuthError("username_taken", "That username is already taken.", 409)
        user = User(
            username=username,
            full_name=full_name.strip()[:128],
            password_hash=hash_password(password, self.settings.password_iterations),
            role=role,
        )
        session.add(user)
        session.flush()
        repo.audit(session, actor, "user_created", f"user={username} role={role}")
        return user

    def update_user(
        self,
        session: Session,
        user_id: int,
        *,
        actor: User,
        role: str | None = None,
        is_active: bool | None = None,
        full_name: str | None = None,
    ) -> User:
        user = session.get(User, user_id)
        if user is None:
            raise AuthError("not_found", "User not found.", 404)
        demoting_admin = user.role == "admin" and (
            (role is not None and role != "admin") or (is_active is False)
        )
        if demoting_admin and repo.count_active_admins(session) <= 1:
            raise AuthError("last_admin", "At least one active administrator must remain.", 409)
        if role is not None:
            if role not in ROLES:
                raise AuthError("invalid_role", f"Role must be one of {', '.join(ROLES)}.")
            user.role = role
        if is_active is not None:
            user.is_active = is_active
            if not is_active:
                self._revoke_all(session, user.id)
        if full_name is not None:
            user.full_name = full_name.strip()[:128]
        repo.audit(
            session, actor.username, "user_updated", f"user={user.username} role={user.role} active={user.is_active}"
        )
        return user

    def admin_reset_password(self, session: Session, user_id: int, new_password: str, actor: User) -> User:
        user = session.get(User, user_id)
        if user is None:
            raise AuthError("not_found", "User not found.", 404)
        problems = password_problems(new_password, user.username)
        if problems:
            raise AuthError("weak_password", "Password " + "; ".join(problems) + ".")
        user.password_hash = hash_password(new_password, self.settings.password_iterations)
        user.failed_attempts = 0
        user.locked_until = None
        user.must_change_password = True
        self._revoke_all(session, user.id)
        repo.audit(session, actor.username, "password_reset", f"user={user.username}")
        return user

    def change_password(self, session: Session, user: User, current: str, new: str, keep_token: str | None) -> None:
        if not verify_password(current, user.password_hash):
            raise AuthError("invalid_credentials", "Current password is incorrect.", 400)
        if current == new:
            raise AuthError("weak_password", "The new password must differ from the current one.")
        problems = password_problems(new, user.username)
        if problems:
            raise AuthError("weak_password", "Password " + "; ".join(problems) + ".")
        user.password_hash = hash_password(new, self.settings.password_iterations)
        user.must_change_password = False
        keep_hash = sha256_hex(keep_token) if keep_token else None
        stmt = update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        if keep_hash:
            stmt = stmt.where(AuthSession.token_hash != keep_hash)
        session.execute(stmt.values(revoked_at=utcnow()))
        repo.audit(session, user.username, "password_changed", "Other sessions revoked")

    # --------------------------------------------------------------- sessions
    def authenticate(self, session: Session, username: str, password: str) -> LoginResult:
        username = (username or "").strip()
        user = repo.get_user_by_username(session, username) if username else None
        now = utcnow()
        generic = AuthError("invalid_credentials", "Invalid username or password.", 401)
        if user is None:
            verify_password(password or "", self._dummy_hash)  # equalise timing
            repo.audit(session, username or "?", "login_failed", "unknown user")
            raise generic
        if user.locked_until and user.locked_until > now:
            minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
            repo.audit(session, user.username, "login_blocked", "account locked")
            raise AuthError(
                "account_locked",
                f"Too many failed attempts. The account is locked for about {minutes} more minute(s).",
                423,
            )
        if not verify_password(password or "", user.password_hash):
            user.failed_attempts += 1
            detail = f"attempt {user.failed_attempts}"
            if user.failed_attempts >= self.settings.max_failed_logins:
                user.locked_until = now + timedelta(minutes=self.settings.lockout_minutes)
                user.failed_attempts = 0
                detail += " - account locked"
            repo.audit(session, user.username, "login_failed", detail)
            raise generic
        if not user.is_active:
            repo.audit(session, user.username, "login_failed", "account disabled")
            raise AuthError("account_disabled", "This account has been disabled by an administrator.", 403)

        user.failed_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        if needs_rehash(user.password_hash, self.settings.password_iterations):
            user.password_hash = hash_password(password, self.settings.password_iterations)
        token = secrets.token_urlsafe(32)
        expires = now + timedelta(hours=self.settings.session_ttl_hours)
        session.add(AuthSession(user_id=user.id, token_hash=sha256_hex(token), expires_at=expires))
        repo.audit(session, user.username, "login_success", "")
        return LoginResult(user=user, token=token, expires_at=expires)

    def resolve_token(self, session: Session, token: str) -> User | None:
        if not token:
            return None
        record = session.scalar(select(AuthSession).where(AuthSession.token_hash == sha256_hex(token)))
        now = utcnow()
        if record is None or record.revoked_at is not None or record.expires_at <= now:
            return None
        user = session.get(User, record.user_id)
        if user is None or not user.is_active:
            return None
        if (now - record.last_seen_at).total_seconds() > 60:
            record.last_seen_at = now
        return user

    def logout(self, session: Session, token: str) -> None:
        record = session.scalar(select(AuthSession).where(AuthSession.token_hash == sha256_hex(token)))
        if record and record.revoked_at is None:
            record.revoked_at = utcnow()
            user = session.get(User, record.user_id)
            repo.audit(session, user.username if user else "?", "logout", "")

    def _revoke_all(self, session: Session, user_id: int) -> None:
        session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )

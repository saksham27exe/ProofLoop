"""Request/response schemas for the REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.utils.timeutil import iso


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---------------------------------------------------------------- auth/users
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field("", max_length=128)


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    is_active: bool
    must_change_password: bool
    created_at: str | None
    last_login_at: str | None

    @classmethod
    def of(cls, u: Any) -> UserOut:
        return cls(id=u.id, username=u.username, full_name=u.full_name, role=u.role, is_active=u.is_active,
                   must_change_password=u.must_change_password, created_at=iso(u.created_at), last_login_at=iso(u.last_login_at))


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    user: UserOut


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field("", max_length=128)
    role: Literal["viewer", "analyst", "admin"] = "analyst"


class UserUpdate(BaseModel):
    role: Literal["viewer", "analyst", "admin"] | None = None
    is_active: bool | None = None
    full_name: str | None = Field(None, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=128)


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


# ---------------------------------------------------------------- executions
class ExecutionCreate(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    environment: str = Field(pattern=r"^(database|api|file|src_[a-z0-9]{1,12})$",
                             description="A built-in sandbox (database, api, file) or a connected source (src_<id>)")
    agent_config: Literal["scripted", "llm", "ai_agent"] = "scripted"
    fault_mode: str = Field("none", max_length=48)
    scenario_id: str | None = Field(None, max_length=64)
    reset_sandbox: bool = False


class RunRequest(BaseModel):
    approved: bool = False
    force: bool = Field(False, description="Run even though the task is ambiguous (verdict will be INCONCLUSIVE).")
    verify: bool = Field(False, description="Verify immediately after the run and return the report.")


class ExecutionSummary(BaseModel):
    id: str
    task_description: str
    environment: str
    agent_name: str
    agent_config: str
    execution_status: str
    verdict: str | None
    agent_claimed_status: str | None
    interpreter: str
    scenario_id: str | None
    created_by: str | None
    created_at: str | None
    verified_at: str | None

    @classmethod
    def of(cls, e: Any) -> ExecutionSummary:
        return cls(id=e.id, task_description=e.task_description, environment=e.environment, agent_name=e.agent_name,
                   agent_config=e.agent_config, execution_status=e.execution_status, verdict=e.verdict,
                   agent_claimed_status=e.agent_claimed_status, interpreter=e.interpreter, scenario_id=e.scenario_id,
                   created_by=e.creator.username if e.creator else None, created_at=iso(e.created_at),
                   verified_at=iso(e.verified_at))


class ExecutionList(BaseModel):
    total: int
    items: list[ExecutionSummary]


class ExecutionDetail(ExecutionSummary):
    agent_framework: str
    agent_claim: str | None
    agent_error: str | None
    approved_by: str | None
    plan: dict[str, Any] | None
    durations_ms: dict[str, int | None]
    started_at: str | None
    completed_at: str | None
    report_id: str | None
    harness: dict[str, Any]
    report: dict[str, Any] | None = None
    real_data: bool = False
    backup: dict[str, Any] | None = None


def iso_or_none(dt: datetime | None) -> str | None:
    return iso(dt)

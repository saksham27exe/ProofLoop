"""Verification sessions for *your own* agent (any framework, any language).

Observability tools record what an agent says it did; ProofLoop verifies what actually happened.
A session lets an external agent use that verification without being rewritten:

1. ``create_execution(agent_config="external")`` interprets the task into a verification plan.
2. :meth:`start` takes a backup (connected sources) and the independent *before* snapshot,
   and reserves the environment so nothing else in ProofLoop writes to it meanwhile.
3. The agent works. Either it calls ProofLoop's tools through :meth:`call_tool` (every call is
   checked against the source's write policy and recorded in the hash-chained trace automatically),
   or it changes the data itself and reports its tool calls through :meth:`record_events`.
4. :meth:`finish` records the agent's final claim, takes the *after* snapshot and verifies.

The verdict comes only from the snapshots and the deterministic rules - never from the agent's
claim. An agent that changes data without reporting any tool call gets INCONCLUSIVE ("missing
execution evidence"), exactly like ProofLoop's own agents.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app.collector.trace_collector import TraceCollector
from app.database import repositories as repo
from app.database.models import Execution, User
from app.interpretation.verification_plan import VerificationPlan
from app.services.execution_service import EXTERNAL_SESSION_TTL, ExecutionError, active_external_session
from app.services.policies import PolicyToolbox, load_policy
from app.utils.logging import get_logger
from app.utils.timeutil import iso, utcnow
from app.verification.state_inspector import capture_snapshot

if TYPE_CHECKING:
    from app.services.execution_service import ExecutionService

log = get_logger("external_sessions")
SESSION_TTL = EXTERNAL_SESSION_TTL
MAX_EVENTS_PER_REQUEST = 200


class SessionStart(BaseModel):
    approved: bool = False
    force: bool = Field(False, description="Start even though the task is ambiguous (verdict will be INCONCLUSIVE).")
    agent_name: str = Field("external-agent", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 ._:/\-]+$")
    framework: str = Field("custom", min_length=1, max_length=60, pattern=r"^[A-Za-z0-9 ._:/\-]+$")


class ToolCallRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ReportedEvent(BaseModel):
    """A tool call the agent made on its own (outside ProofLoop's tools), or a free-text note."""

    kind: Literal["tool", "note"] = "tool"
    tool: str | None = Field(None, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    status: Literal["success", "error"] = "success"
    message: str = Field("", max_length=2000)


class EventsRequest(BaseModel):
    events: list[ReportedEvent] = Field(min_length=1, max_length=MAX_EVENTS_PER_REQUEST)


class SessionFinish(BaseModel):
    claim: str = Field("", max_length=4000, description="The agent's own final report (recorded, never trusted)")
    claimed_status: Literal["success", "failure"] = "success"
    verify: bool = True


def _key(env: str) -> str:
    return f"external_active:{env}"


class ExternalSessionService:
    def __init__(self, executions: ExecutionService) -> None:
        self.ex = executions

    # ------------------------------------------------------------------ helpers
    def _load(self, s, exec_id: str) -> Execution:
        ex = s.get(Execution, exec_id)
        if ex is None:
            raise ExecutionError("not_found", "Execution not found.", 404)
        if ex.agent_config != "external":
            raise ExecutionError("not_external", "This execution is not for an external agent "
                                 "(create it with agent_config='external').", 409)
        return ex

    def _require_running(self, s, exec_id: str) -> tuple[Execution, dict[str, Any]]:
        ex = self._load(s, exec_id)
        active = active_external_session(s, ex.environment)
        if ex.execution_status != "running" or not active or active.get("execution_id") != exec_id:
            raise ExecutionError("session_not_running", f"No running session for {exec_id} (status: {ex.execution_status}).", 409)
        return ex, active

    def _expire_if_stale(self, s, env: str) -> None:
        active = active_external_session(s, env, include_expired=True)
        if not active:
            return
        try:
            age = utcnow() - datetime.fromisoformat(str(active.get("started_at")).rstrip("Z"))
        except (ValueError, TypeError):
            age = SESSION_TTL
        if age >= SESSION_TTL:
            old = s.get(Execution, active.get("execution_id"))
            if old is not None and old.execution_status == "running":
                old.execution_status = "aborted"
                old.agent_error = "Session expired without finishing."
            repo.set_setting(s, _key(env), None)
            repo.audit(s, "system", "external_session_expired", f"{active.get('execution_id')} on {env}")

    def tool_specs(self, env_name: str) -> list[dict[str, Any]]:
        """OpenAI-style function definitions, ready to hand to any tool-calling framework."""
        env = self.ex.registry.get(env_name)
        return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in env.toolbox().specs]

    # -------------------------------------------------------------------- start
    def start(self, exec_id: str, body: SessionStart, user: User | None = None) -> dict[str, Any]:
        with self.ex.db.session() as s:
            ex = self._load(s, exec_id)
            if ex.execution_status not in ("interpreted", "needs_clarification"):
                raise ExecutionError("invalid_state", f"Execution is already {ex.execution_status}.", 409)
            plan = VerificationPlan.model_validate(repo.get_plan(s, exec_id).plan_data)  # type: ignore[union-attr]
            if ex.execution_status == "needs_clarification" and not body.force:
                raise ExecutionError("clarification_required", "The task is ambiguous: " + " ".join(plan.ambiguities)
                                     + " Rephrase it, or start with force=true to see an INCONCLUSIVE verdict.", 409)
            if plan.requires_approval and not body.approved:
                raise ExecutionError("approval_required", plan.approval_reason or "This operation requires approval.", 409)
            self._expire_if_stale(s, ex.environment)
            active = active_external_session(s, ex.environment)
            if active:
                raise ExecutionError("session_active", f"Another session ({active['execution_id']}) is running on this "
                                     "environment. Finish or abort it first.", 409)
            env_name, instruction = ex.environment, ex.task_description
            ex.agent_name, ex.agent_framework = body.agent_name, body.framework
            if plan.requires_approval:
                ex.approved_by = user.username if user else "system"

        env = self.ex.registry.get(env_name)
        is_source = not self.ex.registry.is_builtin(env_name)
        collector = TraceCollector(self.ex.storage, exec_id)
        with env.lock:
            backup_note = None
            if is_source and self.ex.sources is not None:
                try:
                    bk = self.ex.sources.backup(env, exec_id)
                    backup_note = f"Backup taken before the session ({bk.size_bytes} bytes) - 'Undo run' can restore it."
                except Exception as exc:  # noqa: BLE001 - never let an agent near real data without a backup
                    log.exception("Backup failed for %s", exec_id)
                    raise ExecutionError("backup_failed", f"The safety backup could not be created, so the session was "
                                         f"not started: {exc}", 500) from exc
            before = capture_snapshot(env, "before")
            self.ex._store_snapshot(exec_id, before)
            with self.ex.db.session() as s:
                start_version = self.ex.env_version(s, env_name)
                policy = load_policy(s, env_name)
                ex = s.get(Execution, exec_id)
                ex.execution_status = "running"  # type: ignore[union-attr]
                ex.started_at = utcnow()  # type: ignore[union-attr]
                repo.set_setting(s, _key(env_name), {"execution_id": exec_id, "started_at": iso(utcnow()),
                                                     "start_version": start_version})
                repo.audit(s, user.username if user else "system", "external_session_started",
                           f"{exec_id} on {env_name} by agent {body.agent_name} ({body.framework})")
        collector.started(f"External agent session started by {user.username if user else 'system'}",
                          {"instruction": instruction, "environment": env_name, "agent": body.agent_name,
                           "framework": body.framework, "configuration": "external", "backup": backup_note},
                          source="adapter")
        if not policy.is_empty:
            collector.annotate("Write policy enforced: " + "; ".join(policy.describe()), policy.model_dump())
        return {"execution_id": exec_id, "status": "running", "environment": env_name,
                "before_snapshot_available": before.available, "backup": backup_note,
                "expires_at": iso(utcnow() + SESSION_TTL), "policy": policy.model_dump(),
                "tools": self.tool_specs(env_name), "instruction": instruction}

    # -------------------------------------------------------------------- tools
    def call_tool(self, exec_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        with self.ex.db.session() as s:
            ex, _ = self._require_running(s, exec_id)
            env_name = ex.environment
            policy = load_policy(s, env_name)
        env = self.ex.registry.get(env_name)
        toolbox = PolicyToolbox(env.toolbox(), policy)
        if tool not in {t.name for t in toolbox.specs}:
            raise ExecutionError("unknown_tool", f"Unknown tool {tool!r}. Available: {sorted(t.name for t in toolbox.specs)}.", 404)
        collector = TraceCollector(self.ex.storage, exec_id)
        call_id = f"ext_{secrets.token_hex(5)}"
        with env.lock:
            collector.tool_call(call_id, tool, args)
            result = toolbox.call(tool, args)
            output = dict(result.output)
            if result.error:
                output["error"] = result.error
            collector.tool_response(call_id, tool, result.status, output, result.status_code)
        return {"call_id": call_id, "ok": result.ok, "status": result.status, "status_code": result.status_code,
                "output": result.output, "error": result.error}

    def record_events(self, exec_id: str, events: list[ReportedEvent]) -> dict[str, Any]:
        """Tool calls the agent made itself. Recorded as agent-reported evidence (never as proof)."""
        with self.ex.db.session() as s:
            self._require_running(s, exec_id)
        collector = TraceCollector(self.ex.storage, exec_id)
        ids = []
        for ev in events:
            if ev.kind == "note":
                collector.annotate(f"Agent note: {ev.message}"[:2000])
                continue
            if not ev.tool:
                raise ExecutionError("invalid_event", "A tool event needs a 'tool' name.", 422)
            call_id = f"ext_{secrets.token_hex(5)}"
            collector.tool_call(call_id, ev.tool, ev.arguments)
            collector.tool_response(call_id, ev.tool, ev.status, ev.output if isinstance(ev.output, dict) else {"result": ev.output},
                                    message=ev.message or f"{ev.tool} returned {ev.status} (reported by the agent)")
            ids.append(call_id)
        return {"recorded": len(events), "call_ids": ids}

    # ------------------------------------------------------------------- finish
    def finish(self, exec_id: str, body: SessionFinish, user: User | None = None) -> dict[str, Any]:
        with self.ex.db.session() as s:
            ex, active = self._require_running(s, exec_id)
            env_name, started = ex.environment, ex.started_at
        env = self.ex.registry.get(env_name)
        collector = TraceCollector(self.ex.storage, exec_id)
        claim = body.claim.strip() or "(the agent gave no final report)"
        collector.claim(claim, body.claimed_status)
        with env.lock, self.ex.db.session() as s:
            current = self.ex.env_version(s, env_name)
            start_version = int(active.get("start_version") or 0)
            if current == start_version:
                version = current + 1
                repo.set_setting(s, f"env_version:{env_name}", version)
            else:
                # Something else in ProofLoop changed this environment during the session (e.g. a reset):
                # leave the version mismatch in place so verification reports an INCONCLUSIVE window.
                version = start_version
            ex = s.get(Execution, exec_id)
            duration = int((utcnow() - started).total_seconds() * 1000) if started else None  # type: ignore[operator]
            ex.agent_claim = claim  # type: ignore[union-attr]
            ex.agent_claimed_status = body.claimed_status  # type: ignore[union-attr]
            ex.execution_status = "completed"  # type: ignore[union-attr]
            ex.completed_at = utcnow()  # type: ignore[union-attr]
            ex.execution_ms = duration  # type: ignore[union-attr]
            ex.env_version_after_run = version  # type: ignore[union-attr]
            repo.set_setting(s, _key(env_name), None)
        collector.completed("External agent session finished", {"duration_ms": duration}, source="adapter")
        if not body.verify:
            return {"execution_id": exec_id, "status": "completed"}
        return self.ex.verify_execution(exec_id, user=user)

    def abort(self, exec_id: str, user: User | None = None, reason: str = "") -> dict[str, Any]:
        with self.ex.db.session() as s:
            ex, _ = self._require_running(s, exec_id)
            ex.execution_status = "aborted"
            ex.agent_error = (reason or "Session aborted.")[:500]
            ex.completed_at = utcnow()
            repo.set_setting(s, _key(ex.environment), None)
            repo.audit(s, user.username if user else "system", "external_session_aborted", f"{exec_id}: {reason}"[:300])
        TraceCollector(self.ex.storage, exec_id).annotate(f"Session aborted: {reason or 'no reason given'}")
        return {"execution_id": exec_id, "status": "aborted",
                "note": "No verification was performed. A backup (if any) can still be restored with 'Undo run'."}
